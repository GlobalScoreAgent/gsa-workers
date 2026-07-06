"""JSON-RPC helpers for EVM chain queries."""

from __future__ import annotations

import httpx


class RpcError(Exception):
    """Raised when an RPC endpoint returns an error or invalid payload."""


async def eth_rpc(client: httpx.AsyncClient, url: str, method: str, params: list) -> str:
    """Execute a single JSON-RPC call and return the result field."""
    response = await client.post(
        url,
        json={
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1,
        },
        timeout=5.0,
    )
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, dict):
        raise RpcError("RPC response is not a JSON object")

    if payload.get("error"):
        raise RpcError(str(payload["error"]))

    result = payload.get("result")
    if result is None:
        raise RpcError("RPC response missing result")

    return result


def hex_to_int(hex_value: str) -> int:
    """Convert a hex quantity (0x...) to int."""
    if isinstance(hex_value, int):
        return hex_value
    if not isinstance(hex_value, str):
        raise RpcError("Invalid hex value type")
    return int(hex_value, 16)


def wei_to_eth(wei: int) -> float:
    """Convert wei to ether as a float."""
    return wei / 10**18
