"""Sequential step runner with continue-on-error and global deadline."""

from __future__ import annotations

import logging
import time
from typing import Sequence

from steps.base import Step, StepContext, StepResult

logger = logging.getLogger("on_demand_backfill")


async def run_steps(steps: Sequence[Step], ctx: StepContext) -> list[tuple[str, StepResult]]:
    results: list[tuple[str, StepResult]] = []
    for step in steps:
        if time.monotonic() >= ctx.deadline:
            logger.info("Deadline reached before step=%s; stopping orchestrator", step.name)
            break
        logger.info("Step start name=%s", step.name)
        try:
            result = await step.run(ctx)
        except Exception as exc:
            logger.exception(
                "Step crashed name=%s (%s); continuing to next step",
                step.name,
                exc.__class__.__name__,
            )
            result = StepResult(processed=0, errors=1, skipped_empty=False)
        logger.info(
            "Step done name=%s processed=%s errors=%s skipped_empty=%s",
            step.name,
            result.processed,
            result.errors,
            result.skipped_empty,
        )
        results.append((step.name, result))
    return results
