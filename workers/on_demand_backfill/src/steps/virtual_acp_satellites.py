"""Step: Virtual ACP satellite backfill for needs_satellite_backfill jobs."""

from __future__ import annotations

import asyncio
import logging
import time

from db_common import CLAIM_RETRY_BASE_SECONDS
from steps.base import StepContext, StepResult
from virtual_acp.goldsky import DEFAULT_GOLDSKY_URL, fetch_satellites_for_job
from virtual_acp.mappers import map_all

logger = logging.getLogger("on_demand_backfill")


class VirtualAcpSatellitesStep:
    name = "virtual_acp_satellites"

    async def run(self, ctx: StepContext) -> StepResult:
        goldsky_url = str(ctx.env.get("goldsky_virtual_acp_url") or DEFAULT_GOLDSKY_URL)
        claim_batch_size = int(ctx.env.get("virtual_acp_claim_batch_size", 100))
        claim_stale_seconds = int(ctx.env.get("virtual_acp_claim_stale_seconds", 7200))
        concurrency = int(ctx.env.get("virtual_acp_concurrency", 5))
        claimed_by = ctx.worker_id

        processed = 0
        errors = 0
        any_batch = False
        sem = asyncio.Semaphore(concurrency)

        while True:
            if time.monotonic() >= ctx.deadline:
                logger.info("Time budget reached during virtual_acp_satellites")
                break

            async with ctx.db_lock:
                try:
                    jobs = ctx.db.claim_virtual_acp_satellite_backfill(
                        worker_id=claimed_by,
                        limit=claim_batch_size,
                        stale_seconds=claim_stale_seconds,
                    )
                except Exception as exc:
                    logger.error(
                        "claim_virtual_acp_satellite_backfill failed; retrying: %s", exc
                    )
                    await asyncio.sleep(CLAIM_RETRY_BASE_SECONDS)
                    continue

            if not jobs:
                if not any_batch:
                    logger.info("No pending Virtual ACP satellite backfill. Skipping step.")
                    return StepResult(skipped_empty=True)
                logger.info("Virtual ACP satellite backfill queue empty.")
                break

            any_batch = True
            logger.info(
                "Claimed virtual_acp satellite batch size=%s first=%s last=%s",
                len(jobs),
                jobs[0]["id"],
                jobs[-1]["id"],
            )

            async def handle(job: dict) -> bool:
                async with sem:
                    job_pk = str(job["id"])
                    try:
                        raw = await fetch_satellites_for_job(
                            ctx.http,
                            url=goldsky_url,
                            job_id=int(job["job_id"]),
                            chain_id=str(job["chain_id"]),
                        )
                        mapped = map_all(raw)
                        async with ctx.db_lock:
                            for entity, rows in mapped.items():
                                n = ctx.db.upsert_virtual_acp_entity_rows(entity, rows)
                                logger.info(
                                    "Upserted virtual_acp entity=%s job=%s rows=%s",
                                    entity,
                                    job_pk,
                                    n,
                                )
                            # Complete even if 0 events (avoids eternal queue).
                            ctx.db.complete_virtual_acp_satellite_backfill([job_pk])
                        logger.info("Done virtual_acp satellite job=%s", job_pk)
                        return True
                    except Exception as exc:
                        logger.warning(
                            "Virtual ACP satellite job=%s failed (no complete): %s: %s",
                            job_pk,
                            exc.__class__.__name__,
                            exc,
                        )
                        return False

            outcomes = await asyncio.gather(*(handle(j) for j in jobs))
            for ok in outcomes:
                processed += 1
                if not ok:
                    errors += 1

        return StepResult(processed=processed, errors=errors, skipped_empty=False)
