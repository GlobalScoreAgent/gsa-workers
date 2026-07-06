"""Alchemy JSON-RPC batch queries (pattern from wallet-transactional-current-batch edge function)."""

from __future__ import annotations

import httpx

from rpc import RpcError, hex_to_int, wei_to_eth


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

    response = await client.post(
        rpc_url,
        headers={"Content-Type": "application/json"},
        json=batch_requests,
        timeout=15.0,
    )
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, list):
        raise RpcError("Alchemy batch response is not a JSON array")

    balance_wei: int | None = None
    nonce: int | None = None

    for item in payload:
        if not isinstance(item, dict):
            continue
        if item.get("error"):
            raise RpcError(str(item["error"]))

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
        raise RpcError("Alchemy batch response missing balance or nonce result")

    return wei_to_eth(balance_wei), nonce
