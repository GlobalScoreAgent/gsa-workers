"""Step: Olas Mech satellite backfill for needs_satellite_backfill mechs."""

from __future__ import annotations

import asyncio
import logging
import time

from db_common import CLAIM_RETRY_BASE_SECONDS
from olas_mech.autonolas import (
    DEFAULT_BASE_URL,
    DEFAULT_GNOSIS_URL,
    fetch_satellites_for_mech,
    url_for_chain,
)
from olas_mech.mappers import map_all
from steps.base import StepContext, StepResult

logger = logging.getLogger("on_demand_backfill")


class OlasMechSatellitesStep:
    name = "olas_mech_satellites"

    async def run(self, ctx: StepContext) -> StepResult:
        base_url = str(ctx.env.get("olas_mech_base_url") or DEFAULT_BASE_URL)
        gnosis_url = str(ctx.env.get("olas_mech_gnosis_url") or DEFAULT_GNOSIS_URL)
        claim_batch_size = int(ctx.env.get("olas_mech_claim_batch_size", 100))
        claim_stale_seconds = int(ctx.env.get("olas_mech_claim_stale_seconds", 7200))
        concurrency = int(ctx.env.get("olas_mech_concurrency", 5))
        claimed_by = ctx.worker_id

        processed = 0
        errors = 0
        any_batch = False
        sem = asyncio.Semaphore(concurrency)

        while True:
            if time.monotonic() >= ctx.deadline:
                logger.info("Time budget reached during olas_mech_satellites")
                break

            async with ctx.db_lock:
                try:
                    mechs = ctx.db.claim_olas_mech_satellite_backfill(
                        worker_id=claimed_by,
                        limit=claim_batch_size,
                        stale_seconds=claim_stale_seconds,
                    )
                except Exception as exc:
                    logger.error(
                        "claim_olas_mech_satellite_backfill failed; retrying: %s", exc
                    )
                    await asyncio.sleep(CLAIM_RETRY_BASE_SECONDS)
                    continue

            if not mechs:
                if not any_batch:
                    logger.info("No pending Olas Mech satellite backfill. Skipping step.")
                    return StepResult(skipped_empty=True)
                logger.info("Olas Mech satellite backfill queue empty.")
                break

            any_batch = True
            logger.info(
                "Claimed olas_mech satellite batch size=%s first=%s last=%s",
                len(mechs),
                mechs[0]["id"],
                mechs[-1]["id"],
            )

            async def handle(mech: dict) -> bool:
                async with sem:
                    mech_pk = str(mech["id"])
                    try:
                        chain_id = str(mech["chain_id"])
                        address = str(mech["address"])
                        url = url_for_chain(
                            chain_id, base_url=base_url, gnosis_url=gnosis_url
                        )
                        raw = await fetch_satellites_for_mech(
                            ctx.http,
                            url=url,
                            address=address,
                            chain_id=chain_id,
                        )
                        mapped = map_all(raw, chain_id=chain_id)
                        async with ctx.db_lock:
                            for entity, rows in mapped.items():
                                n = ctx.db.upsert_olas_mech_entity_rows(entity, rows)
                                logger.info(
                                    "Upserted olas_mech entity=%s mech=%s rows=%s",
                                    entity,
                                    mech_pk,
                                    n,
                                )
                            # Complete even if 0 events (avoids eternal queue).
                            ctx.db.complete_olas_mech_satellite_backfill([mech_pk])
                        logger.info("Done olas_mech satellite mech=%s", mech_pk)
                        return True
                    except Exception as exc:
                        logger.warning(
                            "Olas Mech satellite mech=%s failed (no complete): %s: %s",
                            mech_pk,
                            exc.__class__.__name__,
                            exc,
                        )
                        return False

            outcomes = await asyncio.gather(*(handle(m) for m in mechs))
            for ok in outcomes:
                processed += 1
                if not ok:
                    errors += 1

        return StepResult(processed=processed, errors=errors, skipped_empty=False)
