#!/usr/bin/env python3
"""Discover ERC-20 contracts with balance > 0 per wallet_transactions row."""

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

from alchemy_tokens import fetch_erc20_contracts_with_balance
from db import CLAIM_RETRY_BASE_SECONDS, Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("wallet_token_contracts_discovery")

CLAIMED_BY_PREFIX = "wallet_token_contracts_discovery/gha"


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
    """Stable audit id stored in discovery_contracts_claimed_by."""
    suffix = worker_suffix.strip() or "discovery-a"
    if suffix.startswith(CLAIMED_BY_PREFIX):
        return suffix
    return f"{CLAIMED_BY_PREFIX}:{suffix}"


async def process_row(
    client: httpx.AsyncClient,
    *,
    address: str,
    subdomain: str,
    alchemy_key: str,
) -> list[dict[str, str]]:
    contracts = await fetch_erc20_contracts_with_balance(
        client,
        subdomain=subdomain,
        api_key=alchemy_key,
        address=address,
    )
    return [{"contract_address": c, "source": "alchemy"} for c in contracts]


async def run_job() -> int:
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        logger.error("SUPABASE_DB_URL is required")
        return 1

    alchemy_key = os.environ.get("ALCHEMY_FREE_KEY") or os.environ.get("ALCHEMY_KEY")
    if not alchemy_key:
        logger.error("ALCHEMY_FREE_KEY (or ALCHEMY_KEY) is required")
        return 1

    worker_suffix = env_str("WORKER_ID", "discovery-a")
    claimed_by = build_claimed_by(worker_suffix)
    concurrency = env_int("CONCURRENCY", default=10, minimum=1, maximum=20)
    claim_batch_size = env_int("CLAIM_BATCH_SIZE", default=50, minimum=1)
    claim_stale_seconds = env_int("CLAIM_STALE_SECONDS", default=7200, minimum=60)
    max_runtime_seconds = env_int("MAX_RUNTIME_SECONDS", default=19800, minimum=60)

    db = Database(dsn)
    db.connect()
    logger.info(
        "Started claimed_by=%s concurrency=%s claim_batch_size=%s "
        "claim_stale_seconds=%s max_runtime=%ss",
        claimed_by,
        concurrency,
        claim_batch_size,
        claim_stale_seconds,
        max_runtime_seconds,
    )

    start = time.monotonic()
    processed = 0
    completed = 0
    errors = 0
    sem = asyncio.Semaphore(concurrency)
    db_lock = asyncio.Lock()
    http_limits = httpx.Limits(max_connections=100, max_keepalive_connections=40)

    try:
        async with httpx.AsyncClient(timeout=30.0, limits=http_limits) as http_client:
            while True:
                elapsed = time.monotonic() - start
                if elapsed >= max_runtime_seconds:
                    logger.info(
                        "Time budget reached (%.0fs). Processed=%s completed=%s errors=%s",
                        elapsed,
                        processed,
                        completed,
                        errors,
                    )
                    break

                async with db_lock:
                    try:
                        rows = db.claim_rows(
                            worker_id=claimed_by,
                            limit=claim_batch_size,
                            stale_seconds=claim_stale_seconds,
                        )
                    except Exception as exc:
                        logger.error("Claim failed; will retry next loop: %s", exc)
                        await asyncio.sleep(CLAIM_RETRY_BASE_SECONDS)
                        continue

                if not rows:
                    if processed == 0:
                        logger.info("No pending wallet_transactions rows. Exiting.")
                    else:
                        logger.info("No more pending rows in this run.")
                    break

                logger.info(
                    "Claimed batch size=%s first_id=%s last_id=%s",
                    len(rows),
                    rows[0]["id"],
                    rows[-1]["id"],
                )

                async def handle_row(row: dict) -> tuple[int, bool, int]:
                    row_id = int(row["id"])
                    wallet_id = int(row["wallet_id"])
                    chain_id = int(row["chain_id"])
                    address = str(row["address"]).strip().lower()
                    subdomain = str(row["subdomain_alchemy"]).strip()

                    async with sem:
                        try:
                            contracts = await process_row(
                                http_client,
                                address=address,
                                subdomain=subdomain,
                                alchemy_key=alchemy_key,
                            )
                            async with db_lock:
                                msg = db.upsert_contracts_and_mark_done(
                                    row_id=row_id,
                                    wallet_id=wallet_id,
                                    chain_id=chain_id,
                                    contracts=contracts,
                                )
                            logger.info(
                                "Done wt_id=%s wallet_id=%s chain_id=%s contracts=%s %s",
                                row_id,
                                wallet_id,
                                chain_id,
                                len(contracts),
                                msg,
                            )
                            return row_id, True, len(contracts)
                        except Exception as exc:
                            err_text = f"{exc.__class__.__name__}: {exc}"
                            logger.warning(
                                "Row wt_id=%s wallet_id=%s chain_id=%s failed: %s",
                                row_id,
                                wallet_id,
                                chain_id,
                                err_text,
                            )
                            try:
                                async with db_lock:
                                    db.mark_error(row_id, err_text)
                            except Exception as mark_exc:
                                logger.error(
                                    "mark_error failed wt_id=%s: %s",
                                    row_id,
                                    mark_exc,
                                )
                            return row_id, False, 0

                outcomes = await asyncio.gather(*(handle_row(row) for row in rows))
                for _row_id, ok, _n in outcomes:
                    processed += 1
                    if ok:
                        completed += 1
                    else:
                        errors += 1

    except Exception:
        logger.error("Critical job failure:\n%s", traceback.format_exc())
        return 1
    finally:
        db.close()

    logger.info(
        "Finished claimed_by=%s processed=%s completed=%s errors=%s elapsed=%.0fs",
        claimed_by,
        processed,
        completed,
        errors,
        time.monotonic() - start,
    )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run_job()))


if __name__ == "__main__":
    main()
