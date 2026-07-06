"""Query wallet balance and nonce across all configured chains."""

from __future__ import annotations

import asyncio
from typing import Mapping

import httpx

from alchemy import mask_alchemy_endpoint, query_balance_and_nonce
from networks import CHAIN_ORDER, NETWORKS
from rpc import RpcError, eth_rpc, hex_to_int, wei_to_eth


def _error_result(chain_key: str, error: str, status: str = "error") -> dict:
    net = NETWORKS[chain_key]
    return {
        "key": chain_key,
        "network": net["name"],
        "symbol": net["symbol"],
        "chain_id": net["chain_id"],
        "status": status,
        "balance": None,
        "nonce": None,
        "active": False,
        "rpc_endpoint": None,
        "rpc_source": None,
        "error": error,
    }


async def query_single_network(
    client: httpx.AsyncClient,
    chain_key: str,
    address: str,
    wallet_id: int,
    alchemy_subdomains: Mapping[int, str | None],
    alchemy_key: str | None,
) -> dict:
    """Query balance and nonce for one chain: public RPCs first, then Alchemy."""
    net = NETWORKS[chain_key]

    for rpc in net["rpcs"]:
        try:
            balance_hex, nonce_hex = await asyncio.gather(
                eth_rpc(client, rpc, "eth_getBalance", [address, "latest"]),
                eth_rpc(client, rpc, "eth_getTransactionCount", [address, "latest"]),
            )
            balance = wei_to_eth(hex_to_int(balance_hex))
            nonce = hex_to_int(nonce_hex)
            active = balance > 0 or nonce > 0

            return {
                "key": chain_key,
                "network": net["name"],
                "symbol": net["symbol"],
                "chain_id": net["chain_id"],
                "status": "success",
                "balance": balance,
                "nonce": nonce,
                "active": active,
                "rpc_endpoint": rpc,
                "rpc_source": "public_rpc",
                "error": None,
            }
        except (RpcError, httpx.HTTPError, ValueError, TypeError):
            continue

    subdomain = alchemy_subdomains.get(net["chain_id"])
    if subdomain and alchemy_key:
        try:
            balance, nonce = await query_balance_and_nonce(
                client,
                subdomain,
                alchemy_key,
                address,
                str(wallet_id),
            )
            active = balance > 0 or nonce > 0
            return {
                "key": chain_key,
                "network": net["name"],
                "symbol": net["symbol"],
                "chain_id": net["chain_id"],
                "status": "success",
                "balance": balance,
                "nonce": nonce,
                "active": active,
                "rpc_endpoint": mask_alchemy_endpoint(subdomain),
                "rpc_source": "alchemy",
                "error": None,
            }
        except (RpcError, httpx.HTTPError, ValueError, TypeError) as exc:
            return _error_result(
                chain_key,
                f"Alchemy fallback failed: {exc}",
            )

    if not subdomain:
        return _error_result(
            chain_key,
            "No public RPC succeeded and no Alchemy subdomain configured for this chain.",
        )

    return _error_result(
        chain_key,
        "Failed to connect to all public RPC endpoints and Alchemy fallback.",
    )


async def query_all_chains(
    address: str,
    wallet_id: int,
    alchemy_subdomains: Mapping[int, str | None],
    alchemy_key: str | None,
) -> list[dict]:
    """Query all chains in parallel and return results in stable order."""
    async with httpx.AsyncClient() as client:
        tasks = [
            query_single_network(
                client,
                chain_key,
                address,
                wallet_id,
                alchemy_subdomains,
                alchemy_key,
            )
            for chain_key in CHAIN_ORDER
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    ordered: list[dict] = []
    for chain_key, result in zip(CHAIN_ORDER, results):
        if isinstance(result, Exception):
            ordered.append(
                _error_result(chain_key, f"Exception raised: {result}", status="exception")
            )
        else:
            ordered.append(result)

    return ordered
