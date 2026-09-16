"""Alchemy JSON-RPC with Retry-After / exponential backoff.

Transient failures (429, 5xx, timeouts, JSON-RPC rate-limit, malformed 200 bodies) are
retried in-process and raised as AlchemyTransientError if retries are exhausted. Those
must NOT be persisted as has_*_error. Permanent failures raise AlchemyPermanentError.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger("wallet_holdings_discovery")

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


class AlchemyError(RuntimeError):
    """Base Alchemy client error."""


class AlchemyTransientError(AlchemyError):
    """Rate limit, timeout, or 5xx after retries. Leave the discovery flag pending."""


class AlchemyPermanentError(AlchemyError):
    """4xx other than rate-limit, non-rate-limit JSON-RPC error, or unsupported chain."""


def alchemy_url(subdomain: str, api_key: str) -> str:
    return f"https://{subdomain}.g.alchemy.com/v2/{api_key}"


def set_inflight_limit(n: int | None) -> None:
    """Cap parallel Alchemy HTTP calls. None or <=0 disables the semaphore."""
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


def _error_is_rate_limit(error: Any) -> bool:
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
    timeout: float = 45.0,
) -> Any:
    """POST JSON-RPC and return `result`. Raises transient vs permanent Alchemy errors."""
    sem = _inflight
    if sem is None:
        return await _json_rpc_inner(client, url, method, params, timeout=timeout)
    async with sem:
        return await _json_rpc_inner(client, url, method, params, timeout=timeout)


async def _json_rpc_inner(
    client: httpx.AsyncClient,
    url: str,
    method: str,
    params: list[Any],
    *,
    timeout: float,
) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_exc: Exception | None = None

    for attempt in range(1, RATE_LIMIT_MAX_RETRIES + 1):
        response: httpx.Response | None = None
        try:
            response = await client.post(url, json=payload, timeout=timeout)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
            last_exc = exc
            if attempt >= RATE_LIMIT_MAX_RETRIES:
                raise AlchemyTransientError(
                    f"{method} network/timeout after {attempt} attempts: {exc}"
                ) from exc
            delay = _retry_after_seconds(None, attempt)
            logger.warning(
                "Alchemy %s attempt %s/%s %s; retrying in %.1fs",
                method,
                attempt,
                RATE_LIMIT_MAX_RETRIES,
                exc.__class__.__name__,
                delay,
            )
            await asyncio.sleep(delay)
            continue

        status = response.status_code
        if status in RATE_LIMIT_HTTP_STATUSES or status >= 500:
            last_exc = AlchemyTransientError(f"HTTP {status} for {method}")
            if attempt >= RATE_LIMIT_MAX_RETRIES:
                raise AlchemyTransientError(
                    f"{method} HTTP {status} after {attempt} attempts"
                )
            delay = _retry_after_seconds(response, attempt)
            logger.warning(
                "Alchemy %s attempt %s/%s HTTP %s; retrying in %.1fs",
                method,
                attempt,
                RATE_LIMIT_MAX_RETRIES,
                status,
                delay,
            )
            await asyncio.sleep(delay)
            continue

        if status >= 400:
            raise AlchemyPermanentError(f"HTTP {status} for {method}: {response.text[:300]}")

        malformed: str | None = None
        body: Any = None
        try:
            body = response.json()
        except Exception as exc:
            malformed = f"invalid JSON: {exc}"
        else:
            if not isinstance(body, dict):
                malformed = "body is not a JSON-RPC object"
            elif "error" not in body and "result" not in body:
                malformed = "neither result nor error in body"

        # A 200 with an unusable body is a provider hiccup, not a request we can fix:
        # retry it and leave the flag pending instead of burning the row.
        if malformed is not None:
            last_exc = AlchemyTransientError(f"{method} {malformed}")
            if attempt >= RATE_LIMIT_MAX_RETRIES:
                raise AlchemyTransientError(
                    f"{method} malformed response after {attempt} attempts: {malformed}"
                )
            delay = _retry_after_seconds(response, attempt)
            logger.warning(
                "Alchemy %s attempt %s/%s malformed response (%s); retrying in %.1fs",
                method,
                attempt,
                RATE_LIMIT_MAX_RETRIES,
                malformed,
                delay,
            )
            await asyncio.sleep(delay)
            continue

        rpc_error = body.get("error")
        if rpc_error:
            if _error_is_rate_limit(rpc_error):
                last_exc = AlchemyTransientError(f"{method} rate limit: {rpc_error}")
                if attempt >= RATE_LIMIT_MAX_RETRIES:
                    raise AlchemyTransientError(
                        f"{method} rate limit after {attempt} attempts: {rpc_error}"
                    )
                delay = _retry_after_seconds(response, attempt)
                logger.warning(
                    "Alchemy %s attempt %s/%s JSON-RPC rate limit; retrying in %.1fs",
                    method,
                    attempt,
                    RATE_LIMIT_MAX_RETRIES,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            raise AlchemyPermanentError(f"{method} error: {rpc_error}")

        return body.get("result")

    assert last_exc is not None
    raise AlchemyTransientError(str(last_exc)) from last_exc
