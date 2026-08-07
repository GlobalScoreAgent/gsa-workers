#!/usr/bin/env python3
"""On-demand backfill orchestrator: Ethos + ERC-8183 (+ stubs Virtuals/Olas)."""

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

from db_common import Database
from ethos.goldsky import DEFAULT_GOLDSKY_URL as DEFAULT_ETHOS_GOLDSKY
from ethos.score_api import DEFAULT_ETHOS_API_BASE
from erc8183.goldsky import DEFAULT_GOLDSKY_URL as DEFAULT_ERC8183_GOLDSKY
from orchestrator import run_steps
from steps.base import StepContext
from steps.erc8183_satellites import Erc8183SatellitesStep
from steps.ethos_history import EthosHistoryStep
from steps.ethos_scores import EthosScoresStep
from steps.olas_marketplace import OlasMarketplaceStep
from steps.virtuals_acp import VirtualsAcpStep

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("on_demand_backfill")

CLAIMED_BY_PREFIX = "on_demand_backfill/gha"


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


def build_worker_id(suffix: str) -> str:
    s = suffix.strip() or "backfill-a"
    if s.startswith(CLAIMED_BY_PREFIX):
        return s
    return f"{CLAIMED_BY_PREFIX}:{s}"


async def run_job() -> int:
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        logger.error("SUPABASE_DB_URL is required")
        return 1

    worker_id = build_worker_id(env_str("WORKER_ID", "backfill-a"))
    max_runtime_seconds = env_int("MAX_RUNTIME_SECONDS", default=19800, minimum=60)

    env = {
        "goldsky_ethos_url": env_str("GOLDSKY_ETHOS_URL", DEFAULT_ETHOS_GOLDSKY),
        "ethos_api_base": env_str("ETHOS_API_BASE", DEFAULT_ETHOS_API_BASE),
        "ethos_concurrency": env_int("ETHOS_CONCURRENCY", default=3, minimum=1, maximum=10),
        "ethos_claim_batch_size": env_int("ETHOS_CLAIM_BATCH_SIZE", default=10, minimum=1),
        "ethos_claim_stale_seconds": env_int(
            "ETHOS_CLAIM_STALE_SECONDS", default=7200, minimum=60
        ),
        "score_batch_size": env_int("SCORE_BATCH_SIZE", default=50, minimum=1, maximum=200),
        "score_ttl_days": env_int("SCORE_TTL_DAYS", default=15, minimum=1),
        "score_throttle_ms": env_int("SCORE_THROTTLE_MS", default=200, minimum=0),
        "goldsky_erc8183_url": env_str("GOLDSKY_ERC8183_URL", DEFAULT_ERC8183_GOLDSKY),
        "erc8183_claim_batch_size": env_int(
            "ERC8183_CLAIM_BATCH_SIZE", default=100, minimum=1, maximum=500
        ),
        "erc8183_claim_stale_seconds": env_int(
            "ERC8183_CLAIM_STALE_SECONDS", default=7200, minimum=60
        ),
        "erc8183_concurrency": env_int(
            "ERC8183_CONCURRENCY", default=5, minimum=1, maximum=20
        ),
    }

    db = Database(dsn)
    db.connect()
    start = time.monotonic()
    deadline = start + max_runtime_seconds
    db_lock = asyncio.Lock()
    http_limits = httpx.Limits(max_connections=40, max_keepalive_connections=20)

    steps = [
        EthosHistoryStep(),
        EthosScoresStep(),
        Erc8183SatellitesStep(),
        VirtualsAcpStep(),
        OlasMarketplaceStep(),
    ]

    logger.info(
        "Started worker_id=%s max_runtime=%ss steps=%s",
        worker_id,
        max_runtime_seconds,
        [s.name for s in steps],
    )

    try:
        async with httpx.AsyncClient(timeout=60.0, limits=http_limits) as http_client:
            ctx = StepContext(
                db=db,
                http=http_client,
                db_lock=db_lock,
                worker_id=worker_id,
                deadline=deadline,
                env=env,
            )
            results = await run_steps(steps, ctx)
            for name, result in results:
                logger.info(
                    "Summary step=%s processed=%s errors=%s skipped_empty=%s",
                    name,
                    result.processed,
                    result.errors,
                    result.skipped_empty,
                )
    except Exception:
        logger.error("Critical job failure:\n%s", traceback.format_exc())
        return 1
    finally:
        db.close()

    logger.info("Finished worker_id=%s elapsed=%.0fs", worker_id, time.monotonic() - start)
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run_job()))


if __name__ == "__main__":
    main()
