"""Dune Analytics client for paginated latest query results."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger("dune_queries_import")

DUNE_RESULTS_URL = "https://api.dune.com/api/v1/query/{query_id}/results"
DEFAULT_PAGE_SIZE = 10_000
DEFAULT_PAGE_DELAY_SECONDS = 2.0
HTTP_TIMEOUT_SECONDS = 120.0
RATE_LIMIT_MAX_RETRIES = 5
RATE_LIMIT_STATUS_CODES = {429, 503}


class DuneError(RuntimeError):
    """Raised when Dune API returns an error or unexpected payload."""


def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    """Prefer Retry-After header; otherwise exponential backoff 2, 4, 8, 16, 32."""
    raw = response.headers.get("Retry-After")
    if raw is not None and raw.strip() != "":
        try:
            return max(float(raw), 0.0)
        except ValueError:
            pass
    return float(2**attempt)


def fetch_latest_rows(
    api_key: str,
    query_id: int,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    page_delay_seconds: float = DEFAULT_PAGE_DELAY_SECONDS,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Fetch all rows from the latest result of a Dune query (paginated).

    Paces requests for Free-plan high-limit (~40 rpm). Retries 429/503 with backoff.
    """
    if page_size < 1:
        raise ValueError("page_size must be >= 1")
    if page_delay_seconds < 0:
        raise ValueError("page_delay_seconds must be >= 0")

    owns_client = client is None
    http = client or httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
    headers = {"X-Dune-API-Key": api_key}
    url = DUNE_RESULTS_URL.format(query_id=query_id)

    rows: list[dict[str, Any]] = []
    offset = 0
    page = 0

    try:
        while True:
            if page > 0 and page_delay_seconds > 0:
                logger.info(
                    "Waiting %.1fs before next Dune page (rate-limit pacing)",
                    page_delay_seconds,
                )
                time.sleep(page_delay_seconds)

            page += 1
            params = {"limit": page_size, "offset": offset}
            logger.info(
                "Fetching Dune query %s page %s (offset=%s, limit=%s)",
                query_id,
                page,
                offset,
                page_size,
            )

            response: httpx.Response | None = None
            for attempt in range(1, RATE_LIMIT_MAX_RETRIES + 1):
                response = http.get(url, headers=headers, params=params)
                if response.status_code not in RATE_LIMIT_STATUS_CODES:
                    break
                if attempt >= RATE_LIMIT_MAX_RETRIES:
                    break
                delay = _retry_after_seconds(response, attempt)
                logger.warning(
                    "Dune HTTP %s rate limited; sleeping %.1fs before retry %s/%s",
                    response.status_code,
                    delay,
                    attempt,
                    RATE_LIMIT_MAX_RETRIES,
                )
                time.sleep(delay)

            assert response is not None
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
