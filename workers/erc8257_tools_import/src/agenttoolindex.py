"""HTTP client for agenttoolindex.xyz (ERC-8257 public index)."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger("erc8257_tools_import")

DEFAULT_BASE_URL = "https://agenttoolindex.xyz"
HTTP_TIMEOUT_SECONDS = 60.0
MAX_HTTP_ATTEMPTS = 4
RETRY_BASE_SECONDS = 2.0
TOOLS_LIMIT = 500


class AgentToolIndexError(RuntimeError):
    pass


def _request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
) -> Any:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
        try:
            response = client.request(method, url, params=params)
            if response.status_code in (429, 500, 502, 503, 504):
                raise AgentToolIndexError(
                    f"HTTP {response.status_code} for {url}: {response.text[:200]}"
                )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, AgentToolIndexError) as exc:
            last_exc = exc
            if attempt >= MAX_HTTP_ATTEMPTS:
                break
            delay = RETRY_BASE_SECONDS * attempt
            logger.warning(
                "agenttoolindex %s attempt %s/%s failed (%s); retrying in %.1fs",
                url,
                attempt,
                MAX_HTTP_ATTEMPTS,
                exc,
                delay,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise AgentToolIndexError(str(last_exc)) from last_exc


def fetch_stats(
    client: httpx.Client,
    *,
    base_url: str = DEFAULT_BASE_URL,
) -> dict[str, Any]:
    data = _request_json(client, "GET", f"{base_url.rstrip('/')}/api/stats")
    if not isinstance(data, dict):
        raise AgentToolIndexError("/api/stats did not return an object")
    return data


def fetch_tools_by_status(
    client: httpx.Client,
    status: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    limit: int = TOOLS_LIMIT,
) -> list[dict[str, Any]]:
    data = _request_json(
        client,
        "GET",
        f"{base_url.rstrip('/')}/api/tools",
        params={"status": status, "limit": limit},
    )
    if not isinstance(data, dict):
        raise AgentToolIndexError(f"/api/tools status={status} did not return an object")
    tools = data.get("tools")
    if not isinstance(tools, list):
        raise AgentToolIndexError(f"/api/tools status={status} missing tools array")
    rows: list[dict[str, Any]] = []
    for item in tools:
        if isinstance(item, dict):
            rows.append(item)
    return rows


def fetch_full_dump(
    client: httpx.Client,
    *,
    base_url: str = DEFAULT_BASE_URL,
    limit: int = TOOLS_LIMIT,
) -> list[dict[str, Any]]:
    active = fetch_tools_by_status(
        client, "active", base_url=base_url, limit=limit
    )
    deregistered = fetch_tools_by_status(
        client, "deregistered", base_url=base_url, limit=limit
    )
    if not active:
        raise AgentToolIndexError(
            "active dump returned 0 rows; refusing empty upsert"
        )
    if not deregistered:
        raise AgentToolIndexError(
            "deregistered dump returned 0 rows; refusing empty upsert"
        )

    by_key: dict[tuple[int, int], dict[str, Any]] = {}
    for row in active + deregistered:
        chain_id = row.get("chain_id")
        tool_id = row.get("id")
        if chain_id is None or tool_id is None:
            continue
        by_key[(int(chain_id), int(tool_id))] = row

    merged = list(by_key.values())
    logger.info(
        "Fetched dump active=%s deregistered=%s merged_unique=%s",
        len(active),
        len(deregistered),
        len(merged),
    )
    return merged
