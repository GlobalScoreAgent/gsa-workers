"""Alchemy JSON-RPC with Retry-After / exponential backoff.

Ported from wallet_holdings_discovery/src/alchemy_rpc.py. Rate limits, 5xx and
timeouts are retried in-process and surface as RpcTransientError once retries run
out; those must requeue the wallet on a short clock instead of burning the 30-day
window. Everything else is RpcPermanentError.

The inflight semaphore is shared by both lanes so the whole worker holds a single
Alchemy budget.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger("owner_wallet_monthly")

RATE_LIMIT_MAX_RETRIES = 5
RATE_LIMIT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
RATE_LIMIT_JSONRPC_CODES = {-32029, 429}
RATE_LIMIT_MESSAGE_MARKERS = (
    "429",
    "too many requests",
    "rate limit",
    "rate-limit",
    "over rate",
    "compute units per second",
    "exceeded its compute",
    "monthly capacity",
)
MAX_BACKOFF_SECONDS = 32.0

_inflight: asyncio.Semaphore | None = None


class RpcTransientError(RuntimeError):
    """Rate limit, timeout, or 5xx after retries. Requeue the wallet, do not mark Error."""


class RpcPermanentError(RuntimeError):
    """Malformed response, 4xx other than rate-limit, or non-rate-limit JSON-RPC error."""


def set_inflight_limit(n: int | None) -> None:
    """Cap parallel Alchemy HTTP calls across both lanes. None or <=0 disables it."""
    global _inflight
    if n is None or n <= 0:
        _inflight = None
        return
    _inflight = asyncio.Semaphore(n)


def _retry_after_seconds(response: httpx.Response | None, attempt: int) -> float:
    """Prefer Retry-After header; otherwise exponential backoff 2, 4, 8, 16, 32."""
    if response is not None:
        raw = response.headers.get("Retry-After")
        if raw is not None and raw.strip() != "":
            try:
                return min(max(float(raw), 0.0), MAX_BACKOFF_SECONDS)
            except ValueError:
                pass
    return min(float(2**attempt), MAX_BACKOFF_SECONDS)


def error_is_rate_limit(error: Any) -> bool:
    if isinstance(error, dict):
        code = error.get("code")
        if code in RATE_LIMIT_JSONRPC_CODES or str(code) == "429":
            return True
        message = str(error.get("message") or "").lower()
    else:
        message = str(error).lower()
    return any(marker in message for marker in RATE_LIMIT_MESSAGE_MARKERS)


async def json_rpc(
    client: httpx.AsyncClient,
    url: str,
    method: str,
    params: list[Any],
    *,
    timeout: float = 15.0,
) -> Any:
    """POST a single JSON-RPC call and return `result`."""
    body = await _post_with_retry(
        client,
        url,
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        label=method,
        timeout=timeout,
    )
    if not isinstance(body, dict):
        raise RpcPermanentError(f"Invalid JSON-RPC body for {method}")

    rpc_error = body.get("error")
    if rpc_error:
        if error_is_rate_limit(rpc_error):
            raise RpcTransientError(f"{method} rate limit: {rpc_error}")
        raise RpcPermanentError(f"{method} error: {rpc_error}")

    result = body.get("result")
    if result is None:
        raise RpcPermanentError(f"{method} response missing result")
    return result


async def json_rpc_batch(
    client: httpx.AsyncClient,
    url: str,
    requests: list[dict[str, Any]],
    *,
    label: str = "batch",
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    """POST a JSON-RPC batch and return the raw item list."""
    body = await _post_with_retry(client, url, requests, label=label, timeout=timeout)
    if not isinstance(body, list):
        raise RpcPermanentError(f"{label} batch response is not a JSON array")

    for item in body:
        if isinstance(item, dict) and item.get("error"):
            if error_is_rate_limit(item["error"]):
                raise RpcTransientError(f"{label} rate limit: {item['error']}")
            raise RpcPermanentError(f"{label} error: {item['error']}")
    return body


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    payload: Any,
    *,
    label: str,
    timeout: float,
) -> Any:
    sem = _inflight
    if sem is None:
        return await _post_inner(client, url, payload, label=label, timeout=timeout)
    async with sem:
        return await _post_inner(client, url, payload, label=label, timeout=timeout)


async def _post_inner(
    client: httpx.AsyncClient,
    url: str,
    payload: Any,
    *,
    label: str,
    timeout: float,
) -> Any:
    last_exc: Exception | None = None

    for attempt in range(1, RATE_LIMIT_MAX_RETRIES + 1):
        response: httpx.Response | None = None
        try:
            response = await client.post(url, json=payload, timeout=timeout)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
            last_exc = exc
            if attempt >= RATE_LIMIT_MAX_RETRIES:
                raise RpcTransientError(
                    f"{label} network/timeout after {attempt} attempts: {exc}"
                ) from exc
            delay = _retry_after_seconds(None, attempt)
            logger.warning(
                "Alchemy %s attempt %s/%s %s; retrying in %.1fs",
                label,
                attempt,
                RATE_LIMIT_MAX_RETRIES,
                exc.__class__.__name__,
                delay,
            )
            await asyncio.sleep(delay)
            continue

        status = response.status_code
        if status in RATE_LIMIT_HTTP_STATUSES or status >= 500:
            last_exc = RpcTransientError(f"HTTP {status} for {label}")
            if attempt >= RATE_LIMIT_MAX_RETRIES:
                raise RpcTransientError(f"{label} HTTP {status} after {attempt} attempts")
            delay = _retry_after_seconds(response, attempt)
            logger.warning(
                "Alchemy %s attempt %s/%s HTTP %s; retrying in %.1fs",
                label,
                attempt,
                RATE_LIMIT_MAX_RETRIES,
                status,
                delay,
            )
            await asyncio.sleep(delay)
            continue

        if status >= 400:
            raise RpcPermanentError(f"HTTP {status} for {label}: {response.text[:300]}")

        try:
            return response.json()
        except Exception as exc:
            raise RpcPermanentError(f"Invalid JSON for {label}: {exc}") from exc

    assert last_exc is not None
    raise RpcTransientError(str(last_exc)) from last_exc
