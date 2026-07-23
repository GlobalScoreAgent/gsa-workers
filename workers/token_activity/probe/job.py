#!/usr/bin/env python3
"""Token activity probe — 15d census via public eth_getLogs.

Matrix budget (7 jobs): BSC×3 + Base×2 + ETH×1 + `_rest` flex.
Eth / Base / `_rest` pivot in-process to BSC helper when their queues drain.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from db import CLAIM_RETRY_BASE_SECONDS, Database
from logs_scan import probe_wallet_batch
from networks import NETWORKS
from rpc import RpcClient, RpcError, is_logs_query_too_heavy, is_rate_limit_error, is_soft_release_error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("wallet_token_activity_scan")

CLAIMED_BY_PREFIX = "wallet_token_activity_scan/gha"
REST_CHAIN_SLUG = "_rest"
DEFAULT_REST_CHAINS = "celo,polygon,arbitrum,xlayer,gnosis"
PIVOT_TO_BSC = frozenset({"ethereum", "base", REST_CHAIN_SLUG})
BSC_SLUG = "bsc"


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


def _chunk_rows(rows: list[dict], size: int) -> list[list[dict]]:
    if size <= 0:
        return [rows]
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def _parse_rest_chains(raw: str) -> list[str]:
    out: list[str] = []
    for part in raw.split(","):
        slug = part.strip().lower()
        if not slug:
            continue
        if slug not in NETWORKS:
            raise ValueError(f"Unknown REST_CHAINS entry: {slug}")
        out.append(slug)
    if not out:
        raise ValueError("REST_CHAINS is empty")
    return out


@dataclass
class ChainCtx:
    slug: str
    chain_pk: int
    net: dict[str, Any]
    shard: int
    shards: int
    claimed_by: str
    helper: bool
    serialize_claim: bool
    run_native_gate: bool
    wallet_batch_size: int
    chunk_blocks: int
    chunk_min: int
    chunk_max: int


@dataclass
class RunStats:
    processed: int = 0
    completed: int = 0
    errors: int = 0
    enrich_from_logs: int = 0
    enrich_from_native: int = 0


def _probe_defaults(net: dict[str, Any]) -> tuple[int, int, int, int]:
    default_batch = int(net.get("wallet_batch_size") or 50)
    default_chunk = int(net.get("log_chunk_blocks") or 2000)
    default_chunk_max = int(net.get("log_chunk_max") or 10000)
    chunk_max = default_chunk_max
    if net.get("log_chunk_max") is not None:
        chunk_max = min(chunk_max, int(net["log_chunk_max"]))
    chunk_blocks = min(default_chunk, chunk_max)
    return default_batch, chunk_blocks, 50, chunk_max


async def run_job() -> int:
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        logger.error("SUPABASE_DB_URL is required")
        return 1

    chain_slug = env_str("CHAIN", "")
    shard = env_int("SHARD", default=0, minimum=0)
    shards = env_int("SHARDS", default=1, minimum=1)
    if shard >= shards:
        logger.error("SHARD (%s) must be < SHARDS (%s)", shard, shards)
        return 1

    concurrency = env_int("CONCURRENCY", default=1, minimum=1, maximum=8)
    claim_stale_seconds = env_int("CLAIM_STALE_SECONDS", default=7200, minimum=60)
    max_runtime_seconds = env_int("MAX_RUNTIME_SECONDS", default=19800, minimum=60)
    catchup_max_days = env_int(
        "ACTIVITY_CATCHUP_MAX_DAYS", default=15, minimum=1, maximum=15
    )
    min_interval_ms = env_int("RPC_MIN_INTERVAL_MS", default=400, minimum=0)
    retry_base = float(os.environ.get("RPC_RETRY_BASE_SECONDS") or "2")
    worker_suffix = env_str("WORKER_ID", "a")
    claim_jitter_ms = env_int("CLAIM_JITTER_MS", default=2000, minimum=0, maximum=10000)
    native_gate_every = env_int("NATIVE_GATE_EVERY_N_LOOPS", default=1, minimum=1)

    is_rest = chain_slug == REST_CHAIN_SLUG
    if not is_rest and (not chain_slug or chain_slug not in NETWORKS):
        logger.error(
            "CHAIN must be one of: %s or %s",
            ", ".join(sorted(NETWORKS)),
            REST_CHAIN_SLUG,
        )
        return 1

    can_pivot_bsc = chain_slug in PIVOT_TO_BSC
    if is_rest:
        primary_queue = _parse_rest_chains(env_str("REST_CHAINS", DEFAULT_REST_CHAINS))
    else:
        primary_queue = [chain_slug]

    db = Database(dsn)
    db.connect()
    stats = RunStats()
    start = time.monotonic()
    http_limits = httpx.Limits(
        max_connections=max(20, concurrency * 4),
        max_keepalive_connections=max(10, concurrency * 2),
    )
    db_lock = asyncio.Lock()
    rpc_by_slug: dict[str, RpcClient] = {}

    def build_ctx(slug: str, *, helper: bool) -> ChainCtx:
        net = NETWORKS[slug]
        row = db.resolve_chain(int(net["evm_chain_id"]))
        if not row.get("is_active"):
            raise RuntimeError(f"Chain {slug} is not active in DB")
        batch_default, chunk_blocks, chunk_min, chunk_max = _probe_defaults(net)
        wallet_batch_size = env_int(
            "WALLET_BATCH_SIZE", default=batch_default, minimum=1, maximum=100
        )
        chunk_blocks = env_int("LOG_CHUNK_BLOCKS", default=chunk_blocks, minimum=50)
        chunk_min = env_int("LOG_CHUNK_MIN", default=chunk_min, minimum=1)
        chunk_max = env_int("LOG_CHUNK_MAX", default=chunk_max, minimum=50)
        chunk_blocks = min(chunk_blocks, chunk_max)
        if helper:
            return ChainCtx(
                slug=slug,
                chain_pk=int(row["id"]),
                net=net,
                shard=0,
                shards=1,
                claimed_by=build_claimed_by(f"{slug}-helper", 0, worker_suffix),
                helper=True,
                serialize_claim=True,
                run_native_gate=False,
                wallet_batch_size=wallet_batch_size,
                chunk_blocks=chunk_blocks,
                chunk_min=chunk_min,
                chunk_max=chunk_max,
            )
        return ChainCtx(
            slug=slug,
            chain_pk=int(row["id"]),
            net=net,
            shard=shard,
            shards=shards,
            claimed_by=build_claimed_by(slug, shard, worker_suffix),
            helper=False,
            serialize_claim=slug == BSC_SLUG,
            run_native_gate=slug == BSC_SLUG and shard == 0,
            wallet_batch_size=wallet_batch_size,
            chunk_blocks=chunk_blocks,
            chunk_min=chunk_min,
            chunk_max=chunk_max,
        )

    def rpc_for(http_client: httpx.AsyncClient, slug: str) -> RpcClient:
        if slug not in rpc_by_slug:
            net = NETWORKS[slug]
            rpc_by_slug[slug] = RpcClient(
                http_client,
                list(net["rpcs"]),
                min_interval_ms=min_interval_ms,
                retry_base_seconds=retry_base,
            )
        return rpc_by_slug[slug]

    async def process_chunk(
        ctx: ChainCtx, rpc: RpcClient, chunk: list[dict]
    ) -> tuple[int, int, int]:
        row_ids = [int(r["id"]) for r in chunk]
        try:
            active_wallets, to_block = await probe_wallet_batch(
                rpc,
                wallets=chunk,
                block_time_sec=float(ctx.net["block_time_sec"]),
                catchup_max_days=catchup_max_days,
                chunk_blocks=ctx.chunk_blocks,
                chunk_min=ctx.chunk_min,
                chunk_max=ctx.chunk_max,
            )
            enrich_row_ids = [
                int(r["id"]) for r in chunk if int(r["wallet_id"]) in active_wallets
            ]
            async with db_lock:
                db.mark_probe_done(
                    row_ids=row_ids,
                    last_block=to_block,
                    enqueue_enrich_row_ids=enrich_row_ids,
                )
            logger.info(
                "Probe done chain=%s helper=%s wallets=%s active=%s to_block=%s enrich=%s",
                ctx.slug,
                ctx.helper,
                len(chunk),
                len(active_wallets),
                to_block,
                len(enrich_row_ids),
            )
            return len(chunk), len(chunk), len(enrich_row_ids)
        except Exception as exc:
            err_text = f"{exc.__class__.__name__}: {exc}"
            soft = is_soft_release_error(exc) or is_logs_query_too_heavy(exc)
            if soft:
                logger.warning(
                    "RPC soft-release chain=%s ids=%s; +5m (%s)",
                    ctx.slug,
                    row_ids[:5],
                    err_text,
                )
                try:
                    async with db_lock:
                        db.release_claim(row_ids, delay_seconds=300)
                except Exception as mark_exc:
                    logger.error("release_claim failed: %s", mark_exc)
                # -2 soft release; -1 also shrink batch when range/topic issues.
                if is_logs_query_too_heavy(exc) or (
                    isinstance(exc, RpcError)
                    and ("topic" in str(exc).lower() or "invalid" in str(exc).lower())
                ):
                    return len(chunk), 0, -1
                return len(chunk), 0, -2
            logger.warning(
                "Batch failed chain=%s ids=%s: %s",
                ctx.slug,
                row_ids[:5],
                err_text,
            )
            try:
                async with db_lock:
                    db.mark_error(row_ids, err_text)
            except Exception as mark_exc:
                logger.error("mark_error failed: %s", mark_exc)
            if isinstance(exc, RpcError) and (
                "topic" in str(exc).lower() or "invalid" in str(exc).lower()
            ):
                return len(chunk), 0, -1
            return len(chunk), 0, 0

    async def drain_ctx(http_client: httpx.AsyncClient, ctx: ChainCtx) -> str:
        """Drain due rows until empty or time budget.

        Returns 'empty' | 'budget' | 'error'.
        """
        rpc = rpc_for(http_client, ctx.slug)
        wallet_batch_size = ctx.wallet_batch_size
        claim_limit = wallet_batch_size * concurrency
        loop_n = 0
        native_once = False

        logger.info(
            "Drain start chain=%s helper=%s shard=%s/%s claimed_by=%s "
            "batch=%s concurrency=%s claim_limit=%s serialize=%s native_gate=%s",
            ctx.slug,
            ctx.helper,
            ctx.shard,
            ctx.shards,
            ctx.claimed_by,
            wallet_batch_size,
            concurrency,
            claim_limit,
            ctx.serialize_claim,
            ctx.run_native_gate,
        )

        try:
            async with db_lock:
                n_cleared = db.clear_due_errors(chain_pk=ctx.chain_pk)
            if n_cleared:
                logger.info(
                    "Cleared due error flags count=%s chain=%s",
                    n_cleared,
                    ctx.slug,
                )
        except Exception as exc:
            logger.warning("clear_due_errors failed (continuing): %s", exc)

        while True:
            elapsed = time.monotonic() - start
            if elapsed >= max_runtime_seconds:
                logger.info(
                    "Time budget reached (%.0fs) on chain=%s helper=%s",
                    elapsed,
                    ctx.slug,
                    ctx.helper,
                )
                return "budget"

            loop_n += 1
            if ctx.run_native_gate and (
                (loop_n == 1 and not native_once) or loop_n % native_gate_every == 0
            ):
                try:
                    async with db_lock:
                        n_nat = db.enqueue_enrich_native_deltas(chain_pk=ctx.chain_pk)
                    native_once = True
                    if n_nat:
                        stats.enrich_from_native += n_nat
                        logger.info(
                            "Native gate enqueued enrich count=%s chain=%s",
                            n_nat,
                            ctx.slug,
                        )
                except Exception as exc:
                    logger.warning("Native gate failed (continuing): %s", exc)

            if claim_jitter_ms > 0:
                await asyncio.sleep(random.uniform(0, claim_jitter_ms / 1000.0))

            try:
                async with db_lock:
                    rows = db.claim_rows(
                        worker_id=ctx.claimed_by,
                        chain_pk=ctx.chain_pk,
                        shard=ctx.shard,
                        shards=ctx.shards,
                        limit=claim_limit,
                        stale_seconds=claim_stale_seconds,
                        helper=ctx.helper,
                        serialize_claim=ctx.serialize_claim,
                    )
            except Exception as exc:
                logger.error("Claim failed chain=%s; retry: %s", ctx.slug, exc)
                await asyncio.sleep(CLAIM_RETRY_BASE_SECONDS)
                continue

            if not rows:
                logger.info(
                    "No pending probe rows chain=%s helper=%s",
                    ctx.slug,
                    ctx.helper,
                )
                return "empty"

            chunks = _chunk_rows(rows, wallet_batch_size)
            logger.info(
                "Claimed chain=%s helper=%s size=%s chunks=%s first_id=%s last_id=%s",
                ctx.slug,
                ctx.helper,
                len(rows),
                len(chunks),
                int(rows[0]["id"]),
                int(rows[-1]["id"]),
            )

            sem = asyncio.Semaphore(concurrency)

            async def _gated(chunk: list[dict]) -> tuple[int, int, int]:
                async with sem:
                    return await process_chunk(ctx, rpc, chunk)

            results = await asyncio.gather(
                *[_gated(c) for c in chunks],
                return_exceptions=True,
            )
            shrink = False
            for res in results:
                if isinstance(res, BaseException):
                    logger.error("Chunk task crashed: %s", res)
                    stats.errors += wallet_batch_size
                    stats.processed += wallet_batch_size
                    continue
                proc, ok, enr = res
                stats.processed += proc
                if ok:
                    stats.completed += ok
                elif enr == -2:
                    pass
                else:
                    stats.errors += proc
                if enr == -1:
                    shrink = True
                elif enr > 0:
                    stats.enrich_from_logs += enr

            if shrink and wallet_batch_size > 10:
                wallet_batch_size = max(10, wallet_batch_size // 2)
                claim_limit = wallet_batch_size * concurrency
                logger.warning(
                    "Shrinking WALLET_BATCH_SIZE -> %s claim_limit=%s",
                    wallet_batch_size,
                    claim_limit,
                )

    try:
        logger.info(
            "Started probe mode=%s worker_id=%s primary=%s pivot_bsc=%s "
            "concurrency=%s max_runtime=%ss claim_jitter_ms=%s",
            chain_slug,
            worker_suffix,
            primary_queue,
            can_pivot_bsc,
            concurrency,
            max_runtime_seconds,
            claim_jitter_ms,
        )

        async with httpx.AsyncClient(timeout=30.0, limits=http_limits) as http_client:
            exit_reason = "done"
            for slug in primary_queue:
                ctx = build_ctx(slug, helper=False)
                reason = await drain_ctx(http_client, ctx)
                if reason == "budget":
                    exit_reason = "budget"
                    break
                if reason == "error":
                    exit_reason = "error"
                    break

            if (
                exit_reason == "done"
                and can_pivot_bsc
                and (time.monotonic() - start) < max_runtime_seconds
            ):
                logger.info("Pivot to BSC helper (primary queues drained)")
                bsc_ctx = build_ctx(BSC_SLUG, helper=True)
                reason = await drain_ctx(http_client, bsc_ctx)
                if reason == "budget":
                    exit_reason = "budget"
                elif reason == "empty":
                    logger.info("BSC helper queue empty; exiting")

    except Exception:
        logger.error("Critical job failure:\n%s", traceback.format_exc())
        return 1
    finally:
        db.close()

    logger.info(
        "Finished probe mode=%s processed=%s completed=%s errors=%s "
        "enrich_logs=%s enrich_native=%s elapsed=%.0fs",
        chain_slug,
        stats.processed,
        stats.completed,
        stats.errors,
        stats.enrich_from_logs,
        stats.enrich_from_native,
        time.monotonic() - start,
    )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run_job()))


if __name__ == "__main__":
    main()
