#!/usr/bin/env python3
"""Monthly wallet balance and nonce batch job for erc_8004.wallets."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from address import AddressError, is_valid_evm_address, normalize_address
from db import Database
from query import query_all_chains

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("owner_wallet_nonce_balance_monthly")

STATUS_COMPLETED = "Completed"
STATUS_ERROR = "Error"


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


def build_wallet_payload(
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


def results_to_map(chain_results: list[dict]) -> dict:
    return {item["key"]: item for item in chain_results}


def determine_status(chain_results: list[dict]) -> str:
    successes = sum(1 for item in chain_results if item.get("status") == "success")
    if successes == 0:
        return STATUS_ERROR
    return STATUS_COMPLETED


async def process_wallet(
    wallet_id: int,
    address: str,
    alchemy_subdomains: dict[int, str | None],
    alchemy_key: str | None,
) -> tuple[dict, str]:
    normalized = normalize_address(address)
    chain_results = await query_all_chains(
        normalized,
        wallet_id,
        alchemy_subdomains,
        alchemy_key,
    )
    results_map = results_to_map(chain_results)
    status = determine_status(chain_results)
    payload = build_wallet_payload(normalized, status, results_map)
    return payload, status


async def run_job() -> int:
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        logger.error("SUPABASE_DB_URL is required")
        return 1

    concurrency = env_int("CONCURRENCY", default=15, minimum=1, maximum=20)
    claim_batch_size = env_int("CLAIM_BATCH_SIZE", default=100, minimum=1)
    claim_stale_seconds = env_int("CLAIM_STALE_SECONDS", default=7200, minimum=60)
    max_runtime_seconds = env_int("MAX_RUNTIME_SECONDS", default=19800, minimum=60)
    alchemy_key = os.environ.get("ALCHEMY_KEY") or None

    db = Database(dsn)
    db.connect()
    alchemy_subdomains = db.load_alchemy_subdomains()

    eligible = db.count_eligible_wallets(claim_stale_seconds)
    if eligible == 0:
        logger.info("No eligible wallets. Auto-shutdown.")
        db.close()
        return 0

    logger.info(
        "Started eligible=%s concurrency=%s claim_batch_size=%s "
        "claim_stale_seconds=%s max_runtime=%ss alchemy=%s",
        eligible,
        concurrency,
        claim_batch_size,
        claim_stale_seconds,
        max_runtime_seconds,
        "enabled" if alchemy_key else "disabled",
    )

    start = time.monotonic()
    processed = 0
    completed = 0
    errors = 0
    sem = asyncio.Semaphore(concurrency)
    db_lock = asyncio.Lock()

    try:
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
                wallets = db.claim_wallets(
                    limit=claim_batch_size,
                    stale_seconds=claim_stale_seconds,
                )
            if not wallets:
                if processed == 0:
                    logger.info("No eligible wallets found. Auto-shutdown.")
                else:
                    logger.info("No more eligible wallets in this run.")
                break

            logger.info(
                "Claimed batch size=%s first_id=%s last_id=%s",
                len(wallets),
                wallets[0]["id"],
                wallets[-1]["id"],
            )

            async def handle_wallet(row: dict) -> tuple[int, str]:
                wallet_id = int(row["id"])
                address = str(row["address"])

                async with sem:
                    try:
                        if not is_valid_evm_address(address):
                            raise AddressError(
                                f"Non-EVM or invalid address for wallet id={wallet_id}"
                            )
                        payload, status = await process_wallet(
                            wallet_id,
                            address,
                            alchemy_subdomains,
                            alchemy_key,
                        )
                    except Exception as exc:
                        status = STATUS_ERROR
                        payload = build_wallet_payload(
                            address.strip().lower(),
                            STATUS_ERROR,
                            results=None,
                            error={
                                "type": exc.__class__.__name__,
                                "message": str(exc),
                            },
                        )
                        logger.warning(
                            "Wallet id=%s failed: %s",
                            wallet_id,
                            exc,
                        )

                    async with db_lock:
                        db.save_wallet_result(
                            wallet_id,
                            json.dumps(payload),
                            status,
                        )
                    return wallet_id, status

            outcomes = await asyncio.gather(*(handle_wallet(row) for row in wallets))
            for _wallet_id, status in outcomes:
                processed += 1
                if status == STATUS_COMPLETED:
                    completed += 1
                else:
                    errors += 1

    except Exception:
        logger.error("Critical job failure:\n%s", traceback.format_exc())
        return 1
    finally:
        db.close()

    logger.info(
        "Finished processed=%s completed=%s errors=%s elapsed=%.0fs",
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
