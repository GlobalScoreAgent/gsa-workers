"""Step protocol for on_demand_backfill orchestrator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from db_common import Database


@dataclass
class StepResult:
    processed: int = 0
    errors: int = 0
    skipped_empty: bool = False


@dataclass
class StepContext:
    db: Database
    http: httpx.AsyncClient
    db_lock: asyncio.Lock
    worker_id: str
    deadline: float  # time.monotonic() wall
    env: dict[str, Any]


class Step(Protocol):
    name: str

    async def run(self, ctx: StepContext) -> StepResult: ...
