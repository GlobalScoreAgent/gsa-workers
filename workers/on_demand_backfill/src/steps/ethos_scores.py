"""Step: Ethos credibility scores — list due wallets → API → upsert_official_scores."""

from __future__ import annotations

import asyncio
import logging
import time

from db_common import CLAIM_RETRY_BASE_SECONDS
from ethos.score_api import DEFAULT_ETHOS_API_BASE, fetch_scores_bulk
from steps.base import StepContext, StepResult

logger = logging.getLogger("on_demand_backfill")


class EthosScoresStep:
    name = "ethos_scores"

    async def run(self, ctx: StepContext) -> StepResult:
        ethos_api_base = str(ctx.env.get("ethos_api_base") or DEFAULT_ETHOS_API_BASE)
        score_batch_size = int(ctx.env.get("score_batch_size", 50))
        score_ttl_days = int(ctx.env.get("score_ttl_days", 15))
        throttle_ms = int(ctx.env.get("score_throttle_ms", 200))

        processed = 0
        errors = 0
        any_batch = False

        while True:
            if time.monotonic() >= ctx.deadline:
                logger.info("Time budget reached during ethos_scores")
                break

            async with ctx.db_lock:
                try:
                    candidates = ctx.db.list_score_candidates(score_batch_size)
                except Exception as exc:
                    logger.error("list_score_candidates failed; retrying: %s", exc)
                    await asyncio.sleep(CLAIM_RETRY_BASE_SECONDS)
                    continue

            if not candidates:
                if not any_batch:
                    logger.info("No score candidates due. Skipping step.")
                    return StepResult(skipped_empty=True)
                logger.info("No more score candidates in this run.")
                break

            any_batch = True
            addresses = [str(c["address"]).strip().lower() for c in candidates]
            logger.info("Score chunk size=%s", len(candidates))

            try:
                scores = await fetch_scores_bulk(
                    ctx.http,
                    base_url=ethos_api_base,
                    addresses=addresses,
                    throttle_ms=throttle_ms,
                )
                rows: list[dict] = []
                for c in candidates:
                    addr = str(c["address"]).strip().lower()
                    hit = scores.get(addr) or {}
                    rows.append(
                        {
                            "wallet_id": int(c["wallet_id"]),
                            "address": addr,
                            "profile_id": int(c["profile_id"])
                            if c.get("profile_id") is not None
                            else None,
                            "score": hit.get("score"),
                            "level": hit.get("level"),
                            "source": "ethos_api_v2",
                            "last_error": None,
                        }
                    )
                async with ctx.db_lock:
                    n = ctx.db.upsert_official_scores(rows, ttl_days=score_ttl_days)
                logger.info("Score chunk upserted=%s", n)
                processed += len(candidates)
            except Exception as exc:
                err_text = f"{exc.__class__.__name__}: {exc}"
                logger.warning("Score chunk failed (will retry next run): %s", err_text)
                processed += len(candidates)
                errors += len(candidates)

        return StepResult(processed=processed, errors=errors, skipped_empty=False)
