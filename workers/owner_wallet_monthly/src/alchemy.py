"""Alchemy JSON-RPC: latest balance/nonce batch (monthly) and historical reads (origin).

Both lanes reach Alchemy only as a last resort, after every public endpoint failed,
so every call here goes through the shared backoff client.
"""

from __future__ import annotations

import httpx

from backoff import RpcPermanentError, json_rpc, json_rpc_batch
from rpc import block_to_hex, hex_to_int, wei_to_eth

ALCHEMY_TIMEOUT = 15.0


def build_alchemy_rpc_url(subdomain: str, alchemy_key: str) -> str:
    return f"https://{subdomain}.g.alchemy.com/v2/{alchemy_key}"


def mask_alchemy_endpoint(subdomain: str) -> str:
    """Persist endpoint without exposing the API key."""
    return f"https://{subdomain}.g.alchemy.com/v2/***"


async def query_balance_and_nonce(
    client: httpx.AsyncClient,
    subdomain: str,
    alchemy_key: str,
    address: str,
    request_id: str,
) -> tuple[float, int]:
    """
    Query balance and nonce via a single Alchemy JSON-RPC batch POST.

    Mirrors wallet-transactional-current-batch: bal-{id} / non-{id} request ids.
    """
    rpc_url = build_alchemy_rpc_url(subdomain, alchemy_key)
    block_tag = "latest"
    batch_requests = [
        {
            "jsonrpc": "2.0",
            "id": f"bal-{request_id}",
            "method": "eth_getBalance",
            "params": [address, block_tag],
        },
        {
            "jsonrpc": "2.0",
            "id": f"non-{request_id}",
            "method": "eth_getTransactionCount",
            "params": [address, block_tag],
        },
    ]

    payload = await json_rpc_batch(
        client,
        rpc_url,
        batch_requests,
        label="balance_nonce",
        timeout=ALCHEMY_TIMEOUT,
    )

    balance_wei: int | None = None
    nonce: int | None = None

    for item in payload:
        if not isinstance(item, dict):
            continue

        req_id = str(item.get("id", ""))
        result = item.get("result")
        if result is None:
            continue

        value = hex_to_int(result)
        if req_id == f"bal-{request_id}":
            balance_wei = value
        elif req_id == f"non-{request_id}":
            nonce = value

    if balance_wei is None or nonce is None:
        raise RpcPermanentError("Alchemy batch response missing balance or nonce result")

    return wei_to_eth(balance_wei), nonce


class AlchemyRpc:
    """Historical block reads for the origin lane, behind the shared backoff client."""

    def __init__(self, client: httpx.AsyncClient, subdomain: str, alchemy_key: str):
        self._client = client
        self.url = build_alchemy_rpc_url(subdomain, alchemy_key)
        self.subdomain = subdomain

    async def _call(self, method: str, params: list) -> object:
        return await json_rpc(
            self._client,
            self.url,
            method,
            params,
            timeout=ALCHEMY_TIMEOUT,
        )

    async def block_number(self) -> int:
        return hex_to_int(str(await self._call("eth_blockNumber", [])))

    async def get_code(self, address: str, block_num: int) -> str:
        return str(await self._call("eth_getCode", [address, block_to_hex(block_num)]))

    async def get_balance(self, address: str, block_num: int) -> int:
        result = await self._call("eth_getBalance", [address, block_to_hex(block_num)])
        return hex_to_int(str(result))

    async def get_nonce(self, address: str, block_num: int) -> int:
        result = await self._call(
            "eth_getTransactionCount",
            [address, block_to_hex(block_num)],
        )
        return hex_to_int(str(result))

    async def get_block_timestamp(self, block_num: int) -> int:
        result = await self._call("eth_getBlockByNumber", [block_to_hex(block_num), False])
        if not isinstance(result, dict):
            raise RpcPermanentError("Block response is not an object")
        timestamp = result.get("timestamp")
        if timestamp is None:
            raise RpcPermanentError("Block response missing timestamp")
        return hex_to_int(str(timestamp))
