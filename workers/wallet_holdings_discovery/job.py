#!/usr/bin/env python3
"""Unified holdings discovery: contracts → portfolio → LP per wallet_transactions row."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Literal

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from alchemy_rpc import AlchemyTransientError, set_inflight_limit
from alchemy_tokens import fetch_erc20_contracts_with_balance
from db import CLAIM_RETRY_BASE_SECONDS, Database, Stage
from lp_calc import extract_raw_lp_positions, price_lp_positions
from portfolio_calc import calculate_fungible_positions
from pricing import collect_underlying_addresses

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("wallet_holdings_discovery")

CLAIMED_BY_PREFIX = "wallet_holdings_discovery/gha"
RowOutcome = Literal["ok", "transient", "error"]


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
    suffix = worker_suffix.strip() or "discovery-a"
    if suffix.startswith(CLAIMED_BY_PREFIX):
        return suffix
    return f"{CLAIMED_BY_PREFIX}:{suffix}"


def _is_pending(flag: Any) -> bool:
    return flag is not False


def _truthy_error(flag: Any) -> bool:
    return flag is True


async def process_row(
    http_client: httpx.AsyncClient,
    db: Database,
    db_lock: asyncio.Lock,
    row: dict[str, Any],
    *,
    alchemy_key: str,
) -> RowOutcome:
    row_id = int(row["id"])
    wallet_id = int(row["wallet_id"])
    chain_id = int(row["chain_id"])
    address = str(row["address"]).strip().lower()
    subdomain = str(row["subdomain_alchemy"]).strip()

    need_contracts = _is_pending(row["does_need_discovery_contracts"])
    need_portfolio = _is_pending(row["does_need_portfolio_discovery"])
    need_lp = _is_pending(row["does_need_lp_discovery"])
    contracts_ok = (
        row["does_need_discovery_contracts"] is False
        and not _truthy_error(row["has_discovery_contracts_error"])
    )
    portfolio_ok = (
        row["does_need_portfolio_discovery"] is False
        and not _truthy_error(row["has_portfolio_discovery_error"])
    )

    async def _on_fail(stage: Stage, exc: BaseException) -> RowOutcome:
        err_text = f"{exc.__class__.__name__}: {exc}"
        if isinstance(exc, AlchemyTransientError):
            logger.warning(
                "Transient wt_id=%s stage=%s: %s",
                row_id,
                stage,
                err_text,
            )
            async with db_lock:
                db.release_transient(row_id, stage)
            return "transient"
        logger.warning(
            "Permanent wt_id=%s stage=%s: %s",
            row_id,
            stage,
            err_text,
        )
        async with db_lock:
            db.mark_error(row_id, stage, err_text)
        return "error"

    if need_contracts:
        try:
            contracts = await fetch_erc20_contracts_with_balance(
                http_client,
                subdomain=subdomain,
                api_key=alchemy_key,
                address=address,
            )
            rows = [{"contract_address": c, "source": "alchemy"} for c in contracts]
            async with db_lock:
                msg = db.upsert_contracts_and_mark_done(
                    row_id=row_id,
                    wallet_id=wallet_id,
                    chain_id=chain_id,
                    contracts=rows,
                )
            logger.info(
                "Contracts done wt_id=%s wallet_id=%s chain_id=%s n=%s %s",
                row_id,
                wallet_id,
                chain_id,
                len(contracts),
                msg,
            )
            contracts_ok = True
            need_portfolio = True
        except Exception as exc:
            return await _on_fail("contracts", exc)

    if need_portfolio and contracts_ok:
        try:
            async with db_lock:
                known = db.load_contracts(wallet_id, chain_id)
            positions = await calculate_fungible_positions(
                http_client,
                wallet_address=address,
                chain_id=chain_id,
                subdomain=subdomain,
                alchemy_key=alchemy_key,
                contracts=known,
            )
            async with db_lock:
                msg = db.insert_positions_and_mark_done(
                    row_id=row_id,
                    wallet_id=wallet_id,
                    chain_id=chain_id,
                    positions=positions,
                )
            logger.info(
                "Portfolio done wt_id=%s wallet_id=%s chain_id=%s contracts=%s positions=%s %s",
                row_id,
                wallet_id,
                chain_id,
                len(known),
                len(positions),
                msg,
            )
            portfolio_ok = True
            need_lp = True
        except Exception as exc:
            return await _on_fail("portfolio", exc)

    if need_lp and portfolio_ok:
        try:
            async with db_lock:
                pools = db.load_lp_pools(chain_id)
            raw = await extract_raw_lp_positions(
                http_client,
                wallet_address=address,
                chain_id=chain_id,
                subdomain=subdomain,
                alchemy_key=alchemy_key,
                classic_pools=pools,
            )
            underlyings = collect_underlying_addresses(raw)
            async with db_lock:
                db_prices = db.load_token_prices(chain_id, underlyings)
            positions = await price_lp_positions(
                http_client,
                raw,
                chain_id=chain_id,
                db_prices=db_prices,
            )
            async with db_lock:
                msg = db.upsert_lp_and_mark_done(
                    row_id=row_id,
                    wallet_id=wallet_id,
                    chain_id=chain_id,
                    positions=positions,
                )
            logger.info(
                "LP done wt_id=%s wallet_id=%s chain_id=%s pools=%s positions=%s %s",
                row_id,
                wallet_id,
                chain_id,
                len(pools),
                len(positions),
                msg,
            )
        except Exception as exc:
            return await _on_fail("lp", exc)

    logger.info(
        "Done wt_id=%s wallet_id=%s chain_id=%s",
        row_id,
        wallet_id,
        chain_id,
    )
    return "ok"


async def run_job() -> int:
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        logger.error("SUPABASE_DB_URL is required")
        return 1

    alchemy_key = os.environ.get("ALCHEMY_FREE_KEY") or os.environ.get("ALCHEMY_KEY")
    if not alchemy_key:
        logger.error("ALCHEMY_FREE_KEY (or ALCHEMY_KEY) is required")
        return 1

    claimed_by = build_claimed_by(env_str("WORKER_ID", "discovery-a"))
    concurrency = env_int("CONCURRENCY", default=4, minimum=1, maximum=8)
    claim_batch_size = env_int("CLAIM_BATCH_SIZE", default=15, minimum=1)
    claim_stale_seconds = env_int("CLAIM_STALE_SECONDS", default=7200, minimum=60)
    max_runtime_seconds = env_int("MAX_RUNTIME_SECONDS", default=19800, minimum=60)
    inflight = env_int("ALCHEMY_MAX_INFLIGHT", default=concurrency, minimum=1, maximum=16)
    set_inflight_limit(inflight)

    db = Database(dsn)
    db.connect()
    logger.info(
        "Started claimed_by=%s concurrency=%s alchemy_inflight=%s claim_batch_size=%s "
        "claim_stale_seconds=%s max_runtime=%ss",
        claimed_by,
        concurrency,
        inflight,
        claim_batch_size,
        claim_stale_seconds,
        max_runtime_seconds,
    )

    start = time.monotonic()
    processed = 0
    completed = 0
    transients = 0
    errors = 0
    sem = asyncio.Semaphore(concurrency)
    db_lock = asyncio.Lock()
    http_limits = httpx.Limits(max_connections=80, max_keepalive_connections=30)

    try:
        async with httpx.AsyncClient(timeout=45.0, limits=http_limits) as http_client:
            while True:
                elapsed = time.monotonic() - start
                if elapsed >= max_runtime_seconds:
                    logger.info(
                        "Time budget reached (%.0fs). processed=%s completed=%s "
                        "transient=%s errors=%s",
                        elapsed,
                        processed,
                        completed,
                        transients,
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
                        logger.info("No pending holdings discovery rows. Exiting.")
                    else:
                        logger.info("No more pending rows in this run.")
                    break

                logger.info(
                    "Claimed batch size=%s first_id=%s last_id=%s",
                    len(rows),
                    rows[0]["id"],
                    rows[-1]["id"],
                )

                async def handle_row(claimed: dict[str, Any]) -> RowOutcome:
                    async with sem:
                        try:
                            return await process_row(
                                http_client,
                                db,
                                db_lock,
                                claimed,
                                alchemy_key=alchemy_key,
                            )
                        except Exception as exc:
                            logger.error(
                                "Unhandled wt_id=%s: %s",
                                claimed.get("id"),
                                exc,
                            )
                            return "transient"

                outcomes = await asyncio.gather(*(handle_row(row) for row in rows))
                for outcome in outcomes:
                    processed += 1
                    if outcome == "ok":
                        completed += 1
                    elif outcome == "transient":
                        transients += 1
                    else:
                        errors += 1

    except Exception:
        logger.error("Critical job failure:\n%s", traceback.format_exc())
        return 1
    finally:
        db.close()

    logger.info(
        "Finished claimed_by=%s processed=%s completed=%s transient=%s errors=%s elapsed=%.0fs",
        claimed_by,
        processed,
        completed,
        transients,
        errors,
        time.monotonic() - start,
    )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run_job()))


if __name__ == "__main__":
    main()
