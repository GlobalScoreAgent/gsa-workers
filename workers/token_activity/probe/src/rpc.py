"""JSON-RPC helpers with URL rotation for public RPCs."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger("wallet_token_activity_scan")


class RpcError(Exception):
    """Raised when an RPC endpoint returns an error or invalid payload."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def hex_to_int(hex_value: str | int) -> int:
    if isinstance(hex_value, int):
        return hex_value
    if not isinstance(hex_value, str):
        raise RpcError("Invalid hex value type")
    return int(hex_value, 16)


def int_to_hex(value: int) -> str:
    return hex(value)


def address_to_topic(address: str) -> str:
    addr = address.lower().removeprefix("0x")
    if len(addr) != 40:
        raise RpcError(f"Invalid address length: {address}")
    return "0x" + ("0" * 24) + addr


def topic_to_address(topic: str | None) -> str | None:
    if not topic or not isinstance(topic, str):
        return None
    h = topic.lower().removeprefix("0x")
    if len(h) < 40:
        return None
    return "0x" + h[-40:]


def is_rate_limit_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        s in text
        for s in (
            "429",
            "rate limit",
            "too many requests",
            "capacity",
            "over rate",
        )
    )


def is_result_too_large(exc: BaseException) -> bool:
    """Payload/result size limits (too many logs)."""
    text = str(exc).lower()
    return any(
        s in text
        for s in (
            "query returned more than",
            "response is too big",
            "response too large",
            "log response size exceeded",
            "-32005",
        )
    )


def is_block_range_too_large(exc: BaseException) -> bool:
    """Provider rejects fromBlock-toBlock span (e.g. Cloudflare max 800)."""
    text = str(exc).lower()
    return any(
        s in text
        for s in (
            "range too large",
            "max range",
            "block range is too large",
            "exceeds the range",
            "query exceeds max block",
            "fromblock'-'toblock'",
            "fromblock\":\"toblock",
            "-32047",
        )
    )


def is_logs_query_too_heavy(exc: BaseException) -> bool:
    """Caller should shrink eth_getLogs block chunk (do not rotate forever)."""
    return is_result_too_large(exc) or is_block_range_too_large(exc)


def is_hard_endpoint_failure(exc: BaseException) -> bool:
    """Provider is unusable for this run (ban URL; do not rotate back)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (401, 403)
    text = str(exc).lower()
    return any(
        s in text
        for s in (
            "403 forbidden",
            "401 unauthorized",
            "status code 403",
            "status code 401",
            "'403",
            "'401",
        )
    )


class RpcClient:
    """Sticky public RPC client with rotate + per-run blacklist + backoff."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        urls: list[str],
        *,
        min_interval_ms: int = 150,
        retry_base_seconds: float = 1.0,
        max_retries: int | None = None,
        timeout: float = 30.0,
    ):
        if not urls:
            raise ValueError("urls required")
        self._client = client
        self._urls = list(urls)
        self._idx = 0
        self._blacklisted: set[str] = set()
        self._min_interval = max(0, min_interval_ms) / 1000.0
        self._retry_base = max(0.1, retry_base_seconds)
        # Enough attempts to walk the list a few times after bans.
        default_retries = max(8, len(self._urls) * 3)
        self._max_retries = max(1, max_retries if max_retries is not None else default_retries)
        self._timeout = timeout
        self._last_call_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def current_url(self) -> str:
        return self._urls[self._idx]

    def _rotate(self, *, ban_current: bool = False) -> None:
        if ban_current:
            banned = self.current_url
            self._blacklisted.add(banned)
            logger.warning("Blacklisted RPC URL for this run: %s", banned)

        n = len(self._urls)
        for _ in range(n):
            self._idx = (self._idx + 1) % n
            if self._urls[self._idx] not in self._blacklisted:
                logger.info("Rotated RPC URL -> %s", self.current_url)
                return

        # Every URL banned — clear and keep scanning rather than hard-fail the job.
        logger.warning(
            "All %s RPC URLs blacklisted; clearing blacklist to retry",
            n,
        )
        self._blacklisted.clear()
        self._idx = (self._idx + 1) % n
        logger.info("Rotated RPC URL -> %s", self.current_url)

    async def _pace(self) -> None:
        if self._min_interval <= 0:
            return
        now = asyncio.get_event_loop().time()
        wait = self._last_call_at + self._min_interval - now
        if wait > 0:
            await asyncio.sleep(wait)

    async def call(self, method: str, params: list[Any]) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            async with self._lock:
                await self._pace()
                # Skip sticky index if it was blacklisted mid-run.
                if self.current_url in self._blacklisted:
                    self._rotate(ban_current=False)
                url = self.current_url
                self._last_call_at = asyncio.get_event_loop().time()
            try:
                response = await self._client.post(
                    url,
                    json={
                        "jsonrpc": "2.0",
                        "method": method,
                        "params": params,
                        "id": 1,
                    },
                    timeout=self._timeout,
                )
                body_text = response.text
                if response.status_code == 429:
                    raise RpcError(f"HTTP 429 from {url}", retryable=True)
                if response.status_code >= 400:
                    # Some providers put range/limit details in the HTTP body.
                    if is_logs_query_too_heavy(RpcError(body_text)):
                        raise RpcError(body_text, retryable=False)
                    response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise RpcError("RPC response is not a JSON object")
                if payload.get("error"):
                    err = payload["error"]
                    msg = str(err)
                    # Chunk/range limits must bubble to the scanner (shrink), not rotate.
                    if is_logs_query_too_heavy(RpcError(msg)):
                        raise RpcError(msg, retryable=False)
                    raise RpcError(msg, retryable=is_rate_limit_error(msg))
                if "result" not in payload:
                    raise RpcError("RPC response missing result")
                return payload["result"]
            except (httpx.HTTPError, RpcError, ValueError) as exc:
                # Chunk/range limits: bubble immediately so the scanner can shrink.
                if is_logs_query_too_heavy(exc):
                    raise RpcError(str(exc), retryable=False) from exc
                last_exc = exc if isinstance(exc, Exception) else RpcError(str(exc))
                ban = is_hard_endpoint_failure(exc)
                if attempt >= self._max_retries:
                    break
                delay = self._retry_base * (2 ** (attempt - 1))
                delay = min(delay, 8.0)
                logger.warning(
                    "RPC %s via %s failed attempt %s/%s (%s); sleep %.1fs rotate=True ban=%s",
                    method,
                    url,
                    attempt,
                    self._max_retries,
                    exc,
                    delay,
                    ban,
                )
                await asyncio.sleep(delay)
                async with self._lock:
                    self._rotate(ban_current=ban)
        assert last_exc is not None
        raise last_exc

    async def eth_block_number(self) -> int:
        result = await self.call("eth_blockNumber", [])
        return hex_to_int(result)

    async def eth_get_logs(self, filter_obj: dict[str, Any]) -> list[dict[str, Any]]:
        result = await self.call("eth_getLogs", [filter_obj])
        if result is None:
            return []
        if not isinstance(result, list):
            raise RpcError("eth_getLogs result is not a list")
        return result

    async def eth_get_block_timestamp(self, block_number: int) -> int | None:
        result = await self.call(
            "eth_getBlockByNumber",
            [int_to_hex(block_number), False],
        )
        if not isinstance(result, dict):
            return None
        ts = result.get("timestamp")
        if ts is None:
            return None
        return hex_to_int(ts)
