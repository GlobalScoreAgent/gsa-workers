"""Stub: Olas Marketplace backfill — not implemented until dedicated ADR."""

from __future__ import annotations

import logging

from steps.base import StepContext, StepResult

logger = logging.getLogger("on_demand_backfill")


class OlasMarketplaceStep:
    name = "olas_marketplace"

    async def run(self, ctx: StepContext) -> StepResult:
        logger.info("Step olas_marketplace not implemented; skipping")
        return StepResult(skipped_empty=True)
