#!/usr/bin/env python3
"""Per-chain sharded token activity scan via public eth_getLogs (Transfer)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import traceback
from collections import defaultdict
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from db import CLAIM_RETRY_BASE_SECONDS, Database
from logs_scan import scan_wallet_batch
from networks import NETWORKS
from rpc import RpcClient, RpcError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("wallet_token_activity_scan")

CLAIMED_BY_PREFIX = "wallet_token_activity_scan/gha"


def env_int(name: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
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


def build_claimed_by(chain: str, shard: int, worker_suffix: str) -> str:
    suffix = worker_suffix.strip() or "a"
    return f"{CLAIMED_BY_PREFIX}:{chain}:s{shard}:{suffix}"


async def run_job() -> int:
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        logger.error("SUPABASE_DB_URL is required")
        return 1

    chain_slug = env_str("CHAIN", "")
    if not chain_slug or chain_slug not in NETWORKS:
        logger.error(
            "CHAIN must be one of: %s",
            ", ".join(sorted(NETWORKS)),
        )
        return 1

    shard = env_int("SHARD", default=0, minimum=0)
    shards = env_int("SHARDS", default=1, minimum=1)
    if shard >= shards:
        logger.error("SHARD (%s) must be < SHARDS (%s)", shard, shards)
        return 1

    net = NETWORKS[chain_slug]
    # Per-chain defaults (ETH: smaller OR batch + Cloudflare-friendly 800-block chunks).
    default_batch = int(net.get("wallet_batch_size") or 50)
    default_chunk = int(net.get("log_chunk_blocks") or 2000)
    default_chunk_max = int(net.get("log_chunk_max") or 10000)
    wallet_batch_size = env_int(
        "WALLET_BATCH_SIZE", default=default_batch, minimum=1, maximum=100
    )
    claim_stale_seconds = env_int("CLAIM_STALE_SECONDS", default=7200, minimum=60)
    max_runtime_seconds = env_int("MAX_RUNTIME_SECONDS", default=19800, minimum=60)
    catchup_max_days = env_int("ACTIVITY_CATCHUP_MAX_DAYS", default=3, minimum=1, maximum=15)
    chunk_blocks = env_int("LOG_CHUNK_BLOCKS", default=default_chunk, minimum=50)
    chunk_min = env_int("LOG_CHUNK_MIN", default=50, minimum=1)
    chunk_max = env_int("LOG_CHUNK_MAX", default=default_chunk_max, minimum=50)
    # Network hard ceilings always win (e.g. ETH Cloudflare max 800).
    if net.get("log_chunk_max") is not None:
        chunk_max = min(chunk_max, int(net["log_chunk_max"]))
    chunk_blocks = min(chunk_blocks, chunk_max)
    min_interval_ms = env_int("RPC_MIN_INTERVAL_MS", default=150, minimum=0)
    retry_base = float(os.environ.get("RPC_RETRY_BASE_SECONDS") or "1")
    worker_suffix = env_str("WORKER_ID", "a")
    claimed_by = build_claimed_by(chain_slug, shard, worker_suffix)

    db = Database(dsn)
    db.connect()
    chain_row = db.resolve_chain(int(net["evm_chain_id"]))
    if not chain_row.get("is_active"):
        logger.error("Chain %s is not active in DB", chain_slug)
        return 1
    chain_pk = int(chain_row["id"])

    logger.info(
        "Started chain=%s chain_pk=%s shard=%s/%s claimed_by=%s "
        "batch=%s chunk=%s-%s catchup_days=%s max_runtime=%ss rpcs=%s",
        chain_slug,
        chain_pk,
        shard,
        shards,
        claimed_by,
        wallet_batch_size,
        chunk_blocks,
        chunk_max,
        catchup_max_days,
        max_runtime_seconds,
        net["rpcs"],
    )

    start = time.monotonic()
    processed = 0
    completed = 0
    errors = 0
    http_limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)

    try:
        async with httpx.AsyncClient(timeout=30.0, limits=http_limits) as http_client:
            rpc = RpcClient(
                http_client,
                list(net["rpcs"]),
                min_interval_ms=min_interval_ms,
                retry_base_seconds=retry_base,
            )
            while True:
                elapsed = time.monotonic() - start
                if elapsed >= max_runtime_seconds:
                    logger.info(
                        "Time budget reached (%.0fs). processed=%s completed=%s errors=%s",
                        elapsed,
                        processed,
                        completed,
                        errors,
                    )
                    break

                try:
                    rows = db.claim_rows(
                        worker_id=claimed_by,
                        chain_pk=chain_pk,
                        shard=shard,
                        shards=shards,
                        limit=wallet_batch_size,
                        stale_seconds=claim_stale_seconds,
                    )
                except Exception as exc:
                    logger.error("Claim failed; will retry next loop: %s", exc)
                    await asyncio.sleep(CLAIM_RETRY_BASE_SECONDS)
                    continue

                if not rows:
                    if processed == 0:
                        logger.info("No pending rows for this shard. Exiting.")
                    else:
                        logger.info("No more pending rows in this run.")
                    break

                row_ids = [int(r["id"]) for r in rows]
                logger.info(
                    "Claimed batch size=%s first_id=%s last_id=%s",
                    len(rows),
                    row_ids[0],
                    row_ids[-1],
                )

                try:
                    transfers, erc20_flat, nft_flat, to_block = await scan_wallet_batch(
                        rpc,
                        wallets=rows,
                        chain_pk=chain_pk,
                        block_time_sec=float(net["block_time_sec"]),
                        catchup_max_days=catchup_max_days,
                        chunk_blocks=chunk_blocks,
                        chunk_min=chunk_min,
                        chunk_max=chunk_max,
                    )

                    erc20_by_wallet: dict[int, list[dict[str, str]]] = defaultdict(list)
                    for item in erc20_flat:
                        wid = int(item["wallet_id"])
                        erc20_by_wallet[wid].append(
                            {
                                "contract_address": item["contract_address"],
                                "source": item["source"],
                            }
                        )

                    nft_by_wallet: dict[int, list[dict[str, str]]] = defaultdict(list)
                    for item in nft_flat:
                        wid = int(item["wallet_id"])
                        nft_by_wallet[wid].append(
                            {
                                "contract_address": item["contract_address"],
                                "standard": item["standard"],
                                "source": item["source"],
                            }
                        )

                    msg = db.persist_batch_and_mark_done(
                        row_ids=row_ids,
                        chain_pk=chain_pk,
                        last_block=to_block,
                        transfers=transfers,
                        erc20_by_wallet=dict(erc20_by_wallet),
                        nft_by_wallet=dict(nft_by_wallet),
                    )
                    logger.info(
                        "Done batch wallets=%s transfers=%s erc20=%s nft=%s to_block=%s %s",
                        len(rows),
                        len(transfers),
                        len(erc20_flat),
                        len(nft_flat),
                        to_block,
                        msg,
                    )
                    processed += len(rows)
                    completed += len(rows)
                except Exception as exc:
                    err_text = f"{exc.__class__.__name__}: {exc}"
                    logger.warning("Batch failed ids=%s: %s", row_ids[:5], err_text)
                    try:
                        db.mark_error(row_ids, err_text)
                    except Exception as mark_exc:
                        logger.error("mark_error failed: %s", mark_exc)
                    processed += len(rows)
                    errors += len(rows)

                    # Adaptive: if topic OR list rejected, shrink batch for next claims
                    if isinstance(exc, RpcError) and (
                        "topic" in str(exc).lower() or "invalid" in str(exc).lower()
                    ):
                        if wallet_batch_size > 10:
                            wallet_batch_size = max(10, wallet_batch_size // 2)
                            logger.warning(
                                "Shrinking WALLET_BATCH_SIZE -> %s after RPC error",
                                wallet_batch_size,
                            )

    except Exception:
        logger.error("Critical job failure:\n%s", traceback.format_exc())
        return 1
    finally:
        db.close()

    logger.info(
        "Finished chain=%s shard=%s/%s processed=%s completed=%s errors=%s elapsed=%.0fs",
        chain_slug,
        shard,
        shards,
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
