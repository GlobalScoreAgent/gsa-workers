"""Ethos API v2 credibility score client (public / free tier)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger("on_demand_backfill")

DEFAULT_ETHOS_API_BASE = "https://api.ethos.network/api/v2"
ETHOS_CLIENT_HEADER = "gsa-ethos-enrich@1.0"


async def fetch_scores_bulk(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    addresses: list[str],
    throttle_ms: int = 200,
) -> dict[str, dict[str, Any]]:
    """
    POST /score/addresses → map address(lower) → {score?, level?}.

    Missing keys / empty objects are treated as no score (persist NULL).
    """
    if not addresses:
        return {}

    url = f"{base_url.rstrip('/')}/score/addresses"
    headers = {
        "X-Ethos-Client": ETHOS_CLIENT_HEADER,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {"addresses": addresses}
    resp = await client.post(url, headers=headers, json=body)
    if throttle_ms > 0:
        await asyncio.sleep(throttle_ms / 1000.0)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected Ethos score response type: {type(data)}")

    out: dict[str, dict[str, Any]] = {}
    for key, val in data.items():
        addr = str(key).strip().lower()
        if not addr:
            continue
        if not isinstance(val, dict):
            out[addr] = {"score": None, "level": None}
            continue
        score = val.get("score")
        level = val.get("level")
        out[addr] = {
            "score": float(score) if score is not None else None,
            "level": str(level) if level is not None else None,
        }
    return out
