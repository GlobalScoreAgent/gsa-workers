"""Stub: Virtuals ACP backfill — not implemented until dedicated ADR."""

from __future__ import annotations

import logging

from steps.base import StepContext, StepResult

logger = logging.getLogger("on_demand_backfill")


class VirtualsAcpStep:
    name = "virtuals_acp"

    async def run(self, ctx: StepContext) -> StepResult:
        logger.info("Step virtuals_acp not implemented; skipping")
        return StepResult(skipped_empty=True)
