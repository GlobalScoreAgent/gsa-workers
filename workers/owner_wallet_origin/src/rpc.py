"""JSON-RPC helpers for EVM chain queries (including historical block tags)."""

from __future__ import annotations

import httpx

HISTORICAL_TIMEOUT = 10.0

PRUNE_KEYWORDS = (
    "prune",
    "trie",
    "history",
    "not available",
    "missing",
    "height is too low",
)


class RpcError(Exception):
    """Raised when an RPC endpoint returns an error or invalid payload."""


def is_prune_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(keyword in msg for keyword in PRUNE_KEYWORDS)


def block_to_hex(block_num: int) -> str:
    return hex(block_num)


def hex_to_int(hex_value: str) -> int:
    if isinstance(hex_value, int):
        return hex_value
    if not isinstance(hex_value, str):
        raise RpcError("Invalid hex value type")
    return int(hex_value, 16)


def has_contract_code(code_hex: str) -> bool:
    if not code_hex or code_hex == "0x":
        return False
    return code_hex not in ("0x0", "0x00")


async def eth_rpc(
    client: httpx.AsyncClient,
    url: str,
    method: str,
    params: list,
    *,
    timeout: float = HISTORICAL_TIMEOUT,
) -> object:
    response = await client.post(
        url,
        json={
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1,
        },
        timeout=timeout,
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


async def eth_block_number(client: httpx.AsyncClient, url: str) -> int:
    result = await eth_rpc(client, url, "eth_blockNumber", [])
    return hex_to_int(str(result))


async def eth_get_code_at_block(
    client: httpx.AsyncClient,
    url: str,
    address: str,
    block_num: int,
) -> str:
    result = await eth_rpc(
        client,
        url,
        "eth_getCode",
        [address, block_to_hex(block_num)],
    )
    return str(result)


async def eth_get_balance_at_block(
    client: httpx.AsyncClient,
    url: str,
    address: str,
    block_num: int,
) -> int:
    result = await eth_rpc(
        client,
        url,
        "eth_getBalance",
        [address, block_to_hex(block_num)],
    )
    return hex_to_int(str(result))


async def eth_get_nonce_at_block(
    client: httpx.AsyncClient,
    url: str,
    address: str,
    block_num: int,
) -> int:
    result = await eth_rpc(
        client,
        url,
        "eth_getTransactionCount",
        [address, block_to_hex(block_num)],
    )
    return hex_to_int(str(result))


async def eth_get_block_timestamp(
    client: httpx.AsyncClient,
    url: str,
    block_num: int,
) -> int:
    result = await eth_rpc(
        client,
        url,
        "eth_getBlockByNumber",
        [block_to_hex(block_num), False],
    )
    if not isinstance(result, dict):
        raise RpcError("Block response is not an object")
    timestamp = result.get("timestamp")
    if timestamp is None:
        raise RpcError("Block response missing timestamp")
    return hex_to_int(str(timestamp))
