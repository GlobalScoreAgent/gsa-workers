"""Dune Analytics client for latest token price query results."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("token_prices_import")

DUNE_RESULTS_URL = "https://api.dune.com/api/v1/query/{query_id}/results"
DEFAULT_PAGE_SIZE = 10_000
HTTP_TIMEOUT_SECONDS = 120.0


class DuneError(RuntimeError):
    """Raised when Dune API returns an error or unexpected payload."""


def fetch_latest_rows(
    api_key: str,
    query_id: int,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Fetch all rows from the latest result of a Dune query (paginated)."""
    if page_size < 1:
        raise ValueError("page_size must be >= 1")

    owns_client = client is None
    http = client or httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
    headers = {"X-Dune-API-Key": api_key}
    url = DUNE_RESULTS_URL.format(query_id=query_id)

    rows: list[dict[str, Any]] = []
    offset = 0
    page = 0

    try:
        while True:
            page += 1
            params = {"limit": page_size, "offset": offset}
            logger.info(
                "Fetching Dune query %s page %s (offset=%s, limit=%s)",
                query_id,
                page,
                offset,
                page_size,
            )
            response = http.get(url, headers=headers, params=params)
            if response.status_code >= 400:
                raise DuneError(
                    f"Dune HTTP {response.status_code}: {response.text[:500]}"
                )

            payload = response.json()
            result = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(result, dict):
                raise DuneError("Dune response missing result object")

            page_rows = result.get("rows")
            if not isinstance(page_rows, list):
                raise DuneError("Dune response missing result.rows array")

            rows.extend(page_rows)
            logger.info(
                "Dune page %s returned %s rows (total so far %s)",
                page,
                len(page_rows),
                len(rows),
            )

            next_offset = payload.get("next_offset")
            if next_offset is None:
                break
            try:
                offset = int(next_offset)
            except (TypeError, ValueError) as exc:
                raise DuneError(f"Invalid next_offset: {next_offset!r}") from exc

            if not page_rows:
                break
    finally:
        if owns_client:
            http.close()

    return rows
