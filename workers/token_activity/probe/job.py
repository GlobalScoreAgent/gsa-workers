#!/usr/bin/env python3
"""Per-chain token activity probe (15d census via public eth_getLogs).

One GHA job per chain (runner_count=1): single claim, then internal CONCURRENCY
to probe sub-batches in parallel.
"""

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
from logs_scan import probe_wallet_batch
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


def _chunk_rows(
    rows: list[dict], size: int
) -> list[list[dict]]:
    if size <= 0:
        return [rows]
    return [rows[i : i + size] for i in range(0, len(rows), size)]


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
    default_batch = int(net.get("wallet_batch_size") or 50)
    default_chunk = int(net.get("log_chunk_blocks") or 2000)
    default_chunk_max = int(net.get("log_chunk_max") or 10000)
    wallet_batch_size = env_int(
        "WALLET_BATCH_SIZE", default=default_batch, minimum=1, maximum=100
    )
    # Parallel getLogs sub-batches after a single claim.
    concurrency = env_int("CONCURRENCY", default=4, minimum=1, maximum=8)
    claim_stale_seconds = env_int("CLAIM_STALE_SECONDS", default=7200, minimum=60)
    max_runtime_seconds = env_int("MAX_RUNTIME_SECONDS", default=19800, minimum=60)
    catchup_max_days = env_int("ACTIVITY_CATCHUP_MAX_DAYS", default=15, minimum=1, maximum=15)
    chunk_blocks = env_int("LOG_CHUNK_BLOCKS", default=default_chunk, minimum=50)
    chunk_min = env_int("LOG_CHUNK_MIN", default=50, minimum=1)
    chunk_max = env_int("LOG_CHUNK_MAX", default=default_chunk_max, minimum=50)
    if net.get("log_chunk_max") is not None:
        chunk_max = min(chunk_max, int(net["log_chunk_max"]))
    chunk_blocks = min(chunk_blocks, chunk_max)
    min_interval_ms = env_int("RPC_MIN_INTERVAL_MS", default=150, minimum=0)
    retry_base = float(os.environ.get("RPC_RETRY_BASE_SECONDS") or "1")
    worker_suffix = env_str("WORKER_ID", "a")
    claimed_by = build_claimed_by(chain_slug, shard, worker_suffix)
    native_gate_every = env_int("NATIVE_GATE_EVERY_N_LOOPS", default=1, minimum=1)

    claim_limit = wallet_batch_size * concurrency

    db = Database(dsn)
    db.connect()
    chain_row = db.resolve_chain(int(net["evm_chain_id"]))
    if not chain_row.get("is_active"):
        logger.error("Chain %s is not active in DB", chain_slug)
        return 1
    chain_pk = int(chain_row["id"])

    logger.info(
        "Started probe census chain=%s chain_pk=%s shard=%s/%s claimed_by=%s "
        "batch=%s concurrency=%s claim_limit=%s chunk=%s-%s catchup_days=%s "
        "max_runtime=%ss rpcs=%s",
        chain_slug,
        chain_pk,
        shard,
        shards,
        claimed_by,
        wallet_batch_size,
        concurrency,
        claim_limit,
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
    enrich_from_logs = 0
    enrich_from_native = 0
    loop_n = 0
    http_limits = httpx.Limits(
        max_connections=max(20, concurrency * 4),
        max_keepalive_connections=max(10, concurrency * 2),
    )
    db_lock = asyncio.Lock()

    async def _process_chunk(
        rpc: RpcClient, chunk: list[dict]
    ) -> tuple[int, int, int]:
        """Returns (processed, completed, enrich_count)."""
        row_ids = [int(r["id"]) for r in chunk]
        try:
            active_wallets, to_block = await probe_wallet_batch(
                rpc,
                wallets=chunk,
                block_time_sec=float(net["block_time_sec"]),
                catchup_max_days=catchup_max_days,
                chunk_blocks=chunk_blocks,
                chunk_min=chunk_min,
                chunk_max=chunk_max,
            )
            enrich_row_ids = [
                int(r["id"])
                for r in chunk
                if int(r["wallet_id"]) in active_wallets
            ]
            async with db_lock:
                db.mark_probe_done(
                    row_ids=row_ids,
                    last_block=to_block,
                    enqueue_enrich_row_ids=enrich_row_ids,
                )
            logger.info(
                "Probe done wallets=%s active=%s to_block=%s enrich=%s",
                len(chunk),
                len(active_wallets),
                to_block,
                len(enrich_row_ids),
            )
            return len(chunk), len(chunk), len(enrich_row_ids)
        except Exception as exc:
            err_text = f"{exc.__class__.__name__}: {exc}"
            logger.warning("Batch failed ids=%s: %s", row_ids[:5], err_text)
            try:
                async with db_lock:
                    db.mark_error(row_ids, err_text)
            except Exception as mark_exc:
                logger.error("mark_error failed: %s", mark_exc)
            if isinstance(exc, RpcError) and (
                "topic" in str(exc).lower() or "invalid" in str(exc).lower()
            ):
                return len(chunk), 0, -1  # enrich=-1 => shrink WALLET_BATCH_SIZE
            return len(chunk), 0, 0

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
                        "Time budget reached (%.0fs). processed=%s completed=%s "
                        "errors=%s enrich_logs=%s enrich_native=%s",
                        elapsed,
                        processed,
                        completed,
                        errors,
                        enrich_from_logs,
                        enrich_from_native,
                    )
                    break

                loop_n += 1
                if shard == 0 and (loop_n == 1 or loop_n % native_gate_every == 0):
                    try:
                        async with db_lock:
                            n_nat = db.enqueue_enrich_native_deltas(chain_pk=chain_pk)
                        if n_nat:
                            enrich_from_native += n_nat
                            logger.info(
                                "Native gate enqueued enrich count=%s chain_pk=%s",
                                n_nat,
                                chain_pk,
                            )
                    except Exception as exc:
                        logger.warning("Native gate failed (continuing): %s", exc)

                try:
                    async with db_lock:
                        rows = db.claim_rows(
                            worker_id=claimed_by,
                            chain_pk=chain_pk,
                            shard=shard,
                            shards=shards,
                            limit=claim_limit,
                            stale_seconds=claim_stale_seconds,
                        )
                except Exception as exc:
                    logger.error("Claim failed; will retry next loop: %s", exc)
                    await asyncio.sleep(CLAIM_RETRY_BASE_SECONDS)
                    continue

                if not rows:
                    if processed == 0:
                        logger.info("No pending probe rows for this shard. Exiting.")
                    else:
                        logger.info("No more pending probe rows in this run.")
                    break

                chunks = _chunk_rows(rows, wallet_batch_size)
                logger.info(
                    "Claimed batch size=%s chunks=%s concurrency=%s first_id=%s last_id=%s",
                    len(rows),
                    len(chunks),
                    concurrency,
                    int(rows[0]["id"]),
                    int(rows[-1]["id"]),
                )

                # Cap parallel probes to CONCURRENCY (chunks may be fewer).
                sem = asyncio.Semaphore(concurrency)

                async def _gated(chunk: list[dict]) -> tuple[int, int, int]:
                    async with sem:
                        return await _process_chunk(rpc, chunk)

                results = await asyncio.gather(
                    *[_gated(c) for c in chunks],
                    return_exceptions=True,
                )
                shrink = False
                for res in results:
                    if isinstance(res, BaseException):
                        logger.error("Chunk task crashed: %s", res)
                        errors += wallet_batch_size
                        processed += wallet_batch_size
                        continue
                    proc, ok, enr = res
                    processed += proc
                    if ok:
                        completed += ok
                    else:
                        errors += proc
                    if enr == -1:
                        shrink = True
                    elif enr > 0:
                        enrich_from_logs += enr

                if shrink and wallet_batch_size > 10:
                    wallet_batch_size = max(10, wallet_batch_size // 2)
                    claim_limit = wallet_batch_size * concurrency
                    logger.warning(
                        "Shrinking WALLET_BATCH_SIZE -> %s claim_limit=%s after RPC error",
                        wallet_batch_size,
                        claim_limit,
                    )

    except Exception:
        logger.error("Critical job failure:\n%s", traceback.format_exc())
        return 1
    finally:
        db.close()

    logger.info(
        "Finished probe chain=%s shard=%s/%s processed=%s completed=%s errors=%s "
        "enrich_logs=%s enrich_native=%s elapsed=%.0fs",
        chain_slug,
        shard,
        shards,
        processed,
        completed,
        errors,
        enrich_from_logs,
        enrich_from_native,
        time.monotonic() - start,
    )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run_job()))


if __name__ == "__main__":
    main()
