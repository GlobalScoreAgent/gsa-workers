#!/usr/bin/env python3
"""Ethos enrich: Fase A history (Goldsky) then Fase B credibility scores (API v2)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import traceback
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from db import CLAIM_RETRY_BASE_SECONDS, Database
from ethos_score_api import DEFAULT_ETHOS_API_BASE, fetch_scores_bulk
from goldsky_ethos import DEFAULT_GOLDSKY_URL, fetch_all_signals
from signal_upsert import map_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("ethos_enrich")

CLAIMED_BY_PREFIX = "ethos_enrich/gha"


def env_int(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        value = default
    else:
        value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def build_claimed_by(worker_suffix: str) -> str:
    suffix = worker_suffix.strip() or "enrich-a"
    if suffix.startswith(CLAIMED_BY_PREFIX):
        return suffix
    return f"{CLAIMED_BY_PREFIX}:{suffix}"


async def process_history_profile(
    client: httpx.AsyncClient,
    db: Database,
    db_lock: asyncio.Lock,
    *,
    goldsky_url: str,
    profile_id: int,
) -> bool:
    raw = await fetch_all_signals(client, url=goldsky_url, profile_id=profile_id)
    mapped = map_all(raw)
    async with db_lock:
        for entity, rows in mapped.items():
            n = db.upsert_entity_rows(entity, rows)
            logger.info(
                "Upserted entity=%s profile_id=%s rows=%s",
                entity,
                profile_id,
                n,
            )
        db.complete_history(profile_id)
    logger.info("Done history profile_id=%s", profile_id)
    return True


async def run_phase_history(
    *,
    db: Database,
    db_lock: asyncio.Lock,
    http_client: httpx.AsyncClient,
    claimed_by: str,
    goldsky_url: str,
    claim_batch_size: int,
    claim_stale_seconds: int,
    concurrency: int,
    start: float,
    max_runtime_seconds: int,
) -> tuple[int, int, int]:
    """Returns (processed, completed, errors). Empty first claim → (0,0,0) and returns."""
    processed = 0
    completed = 0
    errors = 0
    sem = asyncio.Semaphore(concurrency)
    any_batch = False

    while True:
        elapsed = time.monotonic() - start
        if elapsed >= max_runtime_seconds:
            logger.info("Time budget reached during history phase (%.0fs)", elapsed)
            break

        async with db_lock:
            try:
                profile_ids = db.claim_history(
                    worker_id=claimed_by,
                    limit=claim_batch_size,
                    stale_seconds=claim_stale_seconds,
                )
            except Exception as exc:
                logger.error("claim_history failed; retrying: %s", exc)
                await asyncio.sleep(CLAIM_RETRY_BASE_SECONDS)
                continue

        if not profile_ids:
            if not any_batch:
                logger.info("No pending history fetch. Skipping to score phase.")
            else:
                logger.info("History queue empty.")
            break

        any_batch = True
        logger.info(
            "Claimed history batch size=%s first=%s last=%s",
            len(profile_ids),
            profile_ids[0],
            profile_ids[-1],
        )

        async def handle(pid: int) -> tuple[int, bool]:
            async with sem:
                try:
                    await process_history_profile(
                        http_client,
                        db,
                        db_lock,
                        goldsky_url=goldsky_url,
                        profile_id=pid,
                    )
                    return pid, True
                except Exception as exc:
                    logger.warning(
                        "History profile_id=%s failed: %s: %s",
                        pid,
                        exc.__class__.__name__,
                        exc,
                    )
                    return pid, False

        outcomes = await asyncio.gather(*(handle(pid) for pid in profile_ids))
        for _pid, ok in outcomes:
            processed += 1
            if ok:
                completed += 1
            else:
                errors += 1

    return processed, completed, errors


async def run_phase_scores(
    *,
    db: Database,
    db_lock: asyncio.Lock,
    http_client: httpx.AsyncClient,
    ethos_api_base: str,
    score_batch_size: int,
    score_ttl_days: int,
    throttle_ms: int,
    start: float,
    max_runtime_seconds: int,
) -> tuple[int, int, int]:
    """Returns (processed, completed, errors). Empty first list → exit phase."""
    processed = 0
    completed = 0
    errors = 0
    any_batch = False

    while True:
        elapsed = time.monotonic() - start
        if elapsed >= max_runtime_seconds:
            logger.info("Time budget reached during score phase (%.0fs)", elapsed)
            break

        async with db_lock:
            try:
                candidates = db.list_score_candidates(score_batch_size)
            except Exception as exc:
                logger.error("list_score_candidates failed; retrying: %s", exc)
                await asyncio.sleep(CLAIM_RETRY_BASE_SECONDS)
                continue

        if not candidates:
            if not any_batch:
                logger.info("No score candidates due. Exiting.")
            else:
                logger.info("No more score candidates in this run.")
            break

        any_batch = True
        addresses = [str(c["address"]).strip().lower() for c in candidates]
        logger.info("Score chunk size=%s", len(candidates))

        try:
            scores = await fetch_scores_bulk(
                http_client,
                base_url=ethos_api_base,
                addresses=addresses,
                throttle_ms=throttle_ms,
            )
            rows: list[dict] = []
            for c in candidates:
                addr = str(c["address"]).strip().lower()
                hit = scores.get(addr) or {}
                rows.append(
                    {
                        "wallet_id": int(c["wallet_id"]),
                        "address": addr,
                        "profile_id": int(c["profile_id"])
                        if c.get("profile_id") is not None
                        else None,
                        "score": hit.get("score"),
                        "level": hit.get("level"),
                        "source": "ethos_api_v2",
                        "last_error": None,
                    }
                )
            async with db_lock:
                n = db.upsert_official_scores(rows, ttl_days=score_ttl_days)
            logger.info("Score chunk upserted=%s", n)
            processed += len(candidates)
            completed += len(candidates)
        except Exception as exc:
            err_text = f"{exc.__class__.__name__}: {exc}"
            logger.warning("Score chunk failed (will retry next run): %s", err_text)
            processed += len(candidates)
            errors += len(candidates)
            # Do not advance next_eligible_at on transport/API failures.

    return processed, completed, errors


async def run_job() -> int:
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        logger.error("SUPABASE_DB_URL is required")
        return 1

    worker_suffix = env_str("WORKER_ID", "enrich-a")
    claimed_by = build_claimed_by(worker_suffix)
    concurrency = env_int("CONCURRENCY", default=3, minimum=1, maximum=10)
    claim_batch_size = env_int("CLAIM_BATCH_SIZE", default=10, minimum=1)
    claim_stale_seconds = env_int("CLAIM_STALE_SECONDS", default=7200, minimum=60)
    score_batch_size = env_int("SCORE_BATCH_SIZE", default=50, minimum=1, maximum=200)
    score_ttl_days = env_int("SCORE_TTL_DAYS", default=15, minimum=1)
    throttle_ms = env_int("SCORE_THROTTLE_MS", default=200, minimum=0)
    max_runtime_seconds = env_int("MAX_RUNTIME_SECONDS", default=19800, minimum=60)
    goldsky_url = env_str("GOLDSKY_ETHOS_URL", DEFAULT_GOLDSKY_URL)
    ethos_api_base = env_str("ETHOS_API_BASE", DEFAULT_ETHOS_API_BASE)

    db = Database(dsn)
    db.connect()
    logger.info(
        "Started claimed_by=%s concurrency=%s claim_batch=%s score_batch=%s "
        "ttl_days=%s max_runtime=%ss",
        claimed_by,
        concurrency,
        claim_batch_size,
        score_batch_size,
        score_ttl_days,
        max_runtime_seconds,
    )

    start = time.monotonic()
    db_lock = asyncio.Lock()
    http_limits = httpx.Limits(max_connections=40, max_keepalive_connections=20)

    try:
        async with httpx.AsyncClient(timeout=60.0, limits=http_limits) as http_client:
            h_proc, h_ok, h_err = await run_phase_history(
                db=db,
                db_lock=db_lock,
                http_client=http_client,
                claimed_by=claimed_by,
                goldsky_url=goldsky_url,
                claim_batch_size=claim_batch_size,
                claim_stale_seconds=claim_stale_seconds,
                concurrency=concurrency,
                start=start,
                max_runtime_seconds=max_runtime_seconds,
            )
            logger.info(
                "History phase done processed=%s completed=%s errors=%s",
                h_proc,
                h_ok,
                h_err,
            )

            if time.monotonic() - start >= max_runtime_seconds:
                logger.info("Skipping score phase: time budget exhausted")
            else:
                s_proc, s_ok, s_err = await run_phase_scores(
                    db=db,
                    db_lock=db_lock,
                    http_client=http_client,
                    ethos_api_base=ethos_api_base,
                    score_batch_size=score_batch_size,
                    score_ttl_days=score_ttl_days,
                    throttle_ms=throttle_ms,
                    start=start,
                    max_runtime_seconds=max_runtime_seconds,
                )
                logger.info(
                    "Score phase done processed=%s completed=%s errors=%s",
                    s_proc,
                    s_ok,
                    s_err,
                )

    except Exception:
        logger.error("Critical job failure:\n%s", traceback.format_exc())
        return 1
    finally:
        db.close()

    logger.info("Finished claimed_by=%s elapsed=%.0fs", claimed_by, time.monotonic() - start)
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run_job()))


if __name__ == "__main__":
    main()
