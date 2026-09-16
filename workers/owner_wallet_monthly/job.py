#!/usr/bin/env python3
"""Owner wallet batch job: monthly balance/nonce and origin history in two lanes.

Both lanes run concurrently in one process. They share the Postgres connection (under
`db_lock`), the HTTP client and the Alchemy inflight budget, but each keeps its own
claim, clock, payload column, status column and snapshot RPC. Running them in parallel
keeps the cheap monthly lane from queueing behind a heavy origin batch, which can take
over two hours on its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from address import AddressError, is_valid_evm_address, normalize_address
from backoff import set_inflight_limit
from db import CLAIM_RETRY_BASE_SECONDS, DEFAULT_NEXT_SECONDS, Database, Lane
from origin import query_all_chains_origin
from query import query_all_chains

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("owner_wallet_monthly")

STATUS_COMPLETED = "Completed"
STATUS_ERROR = "Error"
STATUS_TRANSIENT = "transient"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class LaneConfig:
    lane: Lane
    concurrency: int
    claim_batch_size: int
    claim_stale_seconds: int
    transient_requeue_seconds: int


@dataclass
class WalletOutcome:
    wallet_id: int
    payload: str | None
    status: str | None
    next_seconds: int
    transient_only: bool


@dataclass
class LaneStats:
    processed: int = 0
    completed: int = 0
    transient: int = 0
    errors: int = 0


def results_to_map(chain_results: list[dict]) -> dict:
    return {item["key"]: item for item in chain_results}


def build_monthly_payload(
    address: str,
    wallet_status: str,
    results: dict | None,
    error: dict | None = None,
) -> dict:
    payload: dict = {
        "address": address,
        "queried_at": utc_now_iso(),
        "wallet_status": wallet_status,
    }
    if error is not None:
        payload["error"] = error
    if results is not None:
        payload["results"] = results
    return payload


def build_origin_payload(address: str, results: dict, error: dict | None = None) -> dict:
    payload: dict = {
        "address": address,
        "queried_at": utc_now_iso(),
        "results": results,
    }
    if error is not None:
        payload["error"] = error
    return payload


def count_outcomes(chain_results: list[dict], ok_statuses: tuple[str, ...]) -> tuple[int, int]:
    ok = sum(1 for item in chain_results if item.get("status") in ok_statuses)
    transient = sum(1 for item in chain_results if item.get("status") == STATUS_TRANSIENT)
    return ok, transient


async def run_monthly_wallet(
    client: httpx.AsyncClient,
    wallet_id: int,
    address: str,
    alchemy_subdomains: dict[int, str | None],
    alchemy_key: str | None,
) -> tuple[dict, str, int, int]:
    normalized = normalize_address(address)
    chain_results = await query_all_chains(
        client,
        normalized,
        wallet_id,
        alchemy_subdomains,
        alchemy_key,
    )
    ok, transient = count_outcomes(chain_results, ("success",))
    status = STATUS_COMPLETED if ok > 0 else STATUS_ERROR
    payload = build_monthly_payload(normalized, status, results_to_map(chain_results))
    return payload, status, ok, transient


async def run_origin_wallet(
    client: httpx.AsyncClient,
    wallet_id: int,
    address: str,
    alchemy_subdomains: dict[int, str | None],
    alchemy_key: str | None,
) -> tuple[dict, str, int, int]:
    normalized = normalize_address(address)
    chain_results = await query_all_chains_origin(
        client,
        normalized,
        alchemy_subdomains,
        alchemy_key,
    )
    ok, transient = count_outcomes(chain_results, ("success", "no_activity"))
    status = STATUS_COMPLETED if ok > 0 else STATUS_ERROR
    payload = build_origin_payload(normalized, results_to_map(chain_results))
    return payload, status, ok, transient


def build_error_outcome(
    lane: Lane,
    wallet_id: int,
    address: str,
    exc: BaseException,
) -> WalletOutcome:
    error = {"type": exc.__class__.__name__, "message": str(exc)}
    normalized = address.strip().lower()
    if lane == "monthly":
        payload = build_monthly_payload(normalized, STATUS_ERROR, results=None, error=error)
    else:
        payload = build_origin_payload(normalized, results={}, error=error)
    return WalletOutcome(
        wallet_id=wallet_id,
        payload=json.dumps(payload),
        status=STATUS_ERROR,
        next_seconds=DEFAULT_NEXT_SECONDS,
        transient_only=False,
    )


async def run_lane(
    cfg: LaneConfig,
    db: Database,
    db_lock: asyncio.Lock,
    http_client: httpx.AsyncClient,
    alchemy_subdomains: dict[int, str | None],
    alchemy_key: str | None,
    start: float,
    max_runtime_seconds: int,
) -> LaneStats:
    lane = cfg.lane
    stats = LaneStats()
    sem = asyncio.Semaphore(cfg.concurrency)
    run_wallet = run_monthly_wallet if lane == "monthly" else run_origin_wallet

    while True:
        elapsed = time.monotonic() - start
        if elapsed >= max_runtime_seconds:
            logger.info(
                "Time budget reached lane=%s (%.0fs). processed=%s completed=%s "
                "transient=%s errors=%s",
                lane,
                elapsed,
                stats.processed,
                stats.completed,
                stats.transient,
                stats.errors,
            )
            break

        async with db_lock:
            try:
                wallets = db.claim_wallets(
                    lane,
                    limit=cfg.claim_batch_size,
                    stale_seconds=cfg.claim_stale_seconds,
                )
            except Exception as exc:
                logger.error("Claim failed lane=%s; will retry next loop: %s", lane, exc)
                await asyncio.sleep(CLAIM_RETRY_BASE_SECONDS)
                continue

        if not wallets:
            if stats.processed == 0:
                logger.info("No eligible wallets lane=%s. Lane done.", lane)
            else:
                logger.info("No more eligible wallets lane=%s in this run.", lane)
            break

        logger.info(
            "Claimed batch lane=%s size=%s first_id=%s last_id=%s",
            lane,
            len(wallets),
            wallets[0]["id"],
            wallets[-1]["id"],
        )

        async def handle_wallet(row: dict) -> WalletOutcome:
            wallet_id = int(row["id"])
            address = str(row["address"])

            async with sem:
                try:
                    if not is_valid_evm_address(address):
                        raise AddressError(
                            f"Non-EVM or invalid address for wallet id={wallet_id}"
                        )
                    payload, status, ok, transient = await run_wallet(
                        http_client,
                        wallet_id,
                        address,
                        alchemy_subdomains,
                        alchemy_key,
                    )
                except Exception as exc:
                    logger.warning("Wallet id=%s lane=%s failed: %s", wallet_id, lane, exc)
                    return build_error_outcome(lane, wallet_id, address, exc)

            if transient > 0 and ok == 0:
                logger.warning(
                    "Transient wallet_id=%s lane=%s chains=%s; requeue in %ss",
                    wallet_id,
                    lane,
                    transient,
                    cfg.transient_requeue_seconds,
                )
                return WalletOutcome(
                    wallet_id=wallet_id,
                    payload=None,
                    status=None,
                    next_seconds=cfg.transient_requeue_seconds,
                    transient_only=True,
                )

            next_seconds = DEFAULT_NEXT_SECONDS
            if transient > 0:
                logger.warning(
                    "Partial transient wallet_id=%s lane=%s chains=%s; requeue in %ss",
                    wallet_id,
                    lane,
                    transient,
                    cfg.transient_requeue_seconds,
                )
                next_seconds = cfg.transient_requeue_seconds

            return WalletOutcome(
                wallet_id=wallet_id,
                payload=json.dumps(payload),
                status=status,
                next_seconds=next_seconds,
                transient_only=False,
            )

        outcomes = await asyncio.gather(*(handle_wallet(row) for row in wallets))
        saved = [o for o in outcomes if not o.transient_only]
        transient_ids = [o.wallet_id for o in outcomes if o.transient_only]

        async with db_lock:
            try:
                db.save_results_batch(
                    lane,
                    [
                        (o.wallet_id, str(o.payload), str(o.status), o.next_seconds)
                        for o in saved
                    ],
                )
                db.requeue_transient(lane, transient_ids, cfg.transient_requeue_seconds)

                completed_ids = [
                    o.wallet_id for o in saved if o.status == STATUS_COMPLETED
                ]
                snapshot_failed = db.apply_snapshots(lane, completed_ids)
            except Exception as exc:
                logger.error(
                    "Save/snapshot failed lane=%s; wallets stay Pending: %s",
                    lane,
                    exc,
                )
                stats.processed += len(outcomes)
                stats.errors += len(outcomes)
                continue

        stats.processed += len(outcomes)
        stats.transient += len(transient_ids)
        for outcome in saved:
            if outcome.status == STATUS_COMPLETED and outcome.wallet_id not in snapshot_failed:
                stats.completed += 1
            else:
                stats.errors += 1

    return stats


async def run_job() -> int:
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        logger.error("SUPABASE_DB_URL is required")
        return 1

    monthly_cfg = LaneConfig(
        lane="monthly",
        concurrency=env_int("MONTHLY_CONCURRENCY", default=15, minimum=1, maximum=20),
        claim_batch_size=env_int("MONTHLY_CLAIM_BATCH_SIZE", default=100, minimum=1),
        claim_stale_seconds=env_int("CLAIM_STALE_SECONDS", default=7200, minimum=60),
        transient_requeue_seconds=env_int(
            "TRANSIENT_REQUEUE_SECONDS", default=3600, minimum=60
        ),
    )
    origin_cfg = LaneConfig(
        lane="origin",
        concurrency=env_int("ORIGIN_CONCURRENCY", default=4, minimum=1, maximum=5),
        claim_batch_size=env_int("ORIGIN_CLAIM_BATCH_SIZE", default=50, minimum=1),
        claim_stale_seconds=monthly_cfg.claim_stale_seconds,
        transient_requeue_seconds=monthly_cfg.transient_requeue_seconds,
    )
    max_runtime_seconds = env_int("MAX_RUNTIME_SECONDS", default=19800, minimum=60)
    skip_eligible_count = env_bool("SKIP_ELIGIBLE_COUNT", default=True)
    alchemy_key = os.environ.get("ALCHEMY_KEY") or None
    inflight = env_int("ALCHEMY_MAX_INFLIGHT", default=8, minimum=1, maximum=32)
    set_inflight_limit(inflight)

    db = Database(dsn)
    db.connect()
    alchemy_subdomains = db.load_alchemy_subdomains()

    eligible: dict[str, int] | None = None
    if not skip_eligible_count:
        eligible = {
            "monthly": db.count_eligible_wallets("monthly"),
            "origin": db.count_eligible_wallets("origin"),
        }
        if sum(eligible.values()) == 0:
            logger.info("No eligible wallets in either lane. Auto-shutdown.")
            db.close()
            return 0

    logger.info(
        "Started eligible=%s monthly_concurrency=%s monthly_batch=%s "
        "origin_concurrency=%s origin_batch=%s alchemy_inflight=%s "
        "claim_stale_seconds=%s transient_requeue=%ss max_runtime=%ss alchemy=%s",
        eligible if eligible is not None else "skipped",
        monthly_cfg.concurrency,
        monthly_cfg.claim_batch_size,
        origin_cfg.concurrency,
        origin_cfg.claim_batch_size,
        inflight,
        monthly_cfg.claim_stale_seconds,
        monthly_cfg.transient_requeue_seconds,
        max_runtime_seconds,
        "enabled" if alchemy_key else "disabled",
    )

    start = time.monotonic()
    db_lock = asyncio.Lock()
    http_limits = httpx.Limits(max_connections=200, max_keepalive_connections=50)

    try:
        async with httpx.AsyncClient(timeout=30.0, limits=http_limits) as http_client:
            monthly_stats, origin_stats = await asyncio.gather(
                run_lane(
                    monthly_cfg,
                    db,
                    db_lock,
                    http_client,
                    alchemy_subdomains,
                    alchemy_key,
                    start,
                    max_runtime_seconds,
                ),
                run_lane(
                    origin_cfg,
                    db,
                    db_lock,
                    http_client,
                    alchemy_subdomains,
                    alchemy_key,
                    start,
                    max_runtime_seconds,
                ),
            )
    except Exception:
        logger.error("Critical job failure:\n%s", traceback.format_exc())
        return 1
    finally:
        db.close()

    logger.info(
        "Finished monthly_processed=%s monthly_completed=%s monthly_transient=%s "
        "monthly_errors=%s origin_processed=%s origin_completed=%s origin_transient=%s "
        "origin_errors=%s elapsed=%.0fs",
        monthly_stats.processed,
        monthly_stats.completed,
        monthly_stats.transient,
        monthly_stats.errors,
        origin_stats.processed,
        origin_stats.completed,
        origin_stats.transient,
        origin_stats.errors,
        time.monotonic() - start,
    )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run_job()))


if __name__ == "__main__":
    main()
