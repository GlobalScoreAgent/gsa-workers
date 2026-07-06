"""Wallet origin / first-activity queries via binary search on historical RPC state."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Protocol

import httpx

from alchemy import AlchemyRpc, mask_alchemy_endpoint
from networks import CHAIN_ORDER, NETWORKS
from rpc import (
    eth_block_number,
    eth_get_balance_at_block,
    eth_get_block_timestamp,
    eth_get_code_at_block,
    eth_get_nonce_at_block,
    has_contract_code,
    is_prune_error,
)


class BlockRpc(Protocol):
    async def block_number(self) -> int: ...
    async def get_code(self, address: str, block_num: int) -> str: ...
    async def get_balance(self, address: str, block_num: int) -> int: ...
    async def get_nonce(self, address: str, block_num: int) -> int: ...
    async def get_block_timestamp(self, block_num: int) -> int: ...


class PublicRpc:
    def __init__(self, client: httpx.AsyncClient, url: str):
        self._client = client
        self.url = url

    async def block_number(self) -> int:
        return await eth_block_number(self._client, self.url)

    async def get_code(self, address: str, block_num: int) -> str:
        return await eth_get_code_at_block(self._client, self.url, address, block_num)

    async def get_balance(self, address: str, block_num: int) -> int:
        return await eth_get_balance_at_block(self._client, self.url, address, block_num)

    async def get_nonce(self, address: str, block_num: int) -> int:
        return await eth_get_nonce_at_block(self._client, self.url, address, block_num)

    async def get_block_timestamp(self, block_num: int) -> int:
        return await eth_get_block_timestamp(self._client, self.url, block_num)


def utc_iso_from_timestamp(timestamp: int) -> str:
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


async def find_first_activity_block(rpc: BlockRpc, address: str) -> tuple[int | None, str | None]:
    latest_block = await rpc.block_number()

    try:
        latest_code = await rpc.get_code(address, latest_block)
        is_contract = has_contract_code(latest_code)
    except Exception:
        is_contract = False

    if is_contract:
        low = 0
        high = latest_block
        deploy_block = latest_block
        while low <= high:
            mid = (low + high) // 2
            try:
                mid_code = await rpc.get_code(address, mid)
                if has_contract_code(mid_code):
                    deploy_block = mid
                    high = mid - 1
                else:
                    low = mid + 1
            except Exception:
                low = mid + 1
        return deploy_block, "Contract Deployment"

    try:
        latest_nonce = await rpc.get_nonce(address, latest_block)
    except Exception:
        latest_nonce = 0

    try:
        latest_bal = await rpc.get_balance(address, latest_block)
    except Exception:
        latest_bal = 0

    if latest_nonce == 0 and latest_bal == 0:
        return None, None

    first_nonce_block = None
    if latest_nonce > 0:
        low = 0
        high = latest_block
        while low <= high:
            mid = (low + high) // 2
            try:
                mid_nonce = await rpc.get_nonce(address, mid)
                if mid_nonce > 0:
                    first_nonce_block = mid
                    high = mid - 1
                else:
                    low = mid + 1
            except Exception:
                low = mid + 1

    first_bal_block = None
    if latest_bal > 0:
        low = 0
        high = latest_block
        while low <= high:
            mid = (low + high) // 2
            try:
                mid_bal = await rpc.get_balance(address, mid)
                if mid_bal > 0:
                    first_bal_block = mid
                    high = mid - 1
                else:
                    low = mid + 1
            except Exception:
                low = mid + 1

    if first_nonce_block is not None and first_bal_block is not None:
        min_block = min(first_nonce_block, first_bal_block)
        act_type = (
            "First Transaction (Sent)"
            if min_block == first_nonce_block
            else "First Balance (Received)"
        )
        return min_block, act_type
    if first_nonce_block is not None:
        return first_nonce_block, "First Transaction (Sent)"
    if first_bal_block is not None:
        return first_bal_block, "First Balance (Received)"
    return None, None


def _error_result(
    chain_key: str,
    error: str,
    *,
    status: str = "error",
    rpc_endpoint: str | None = None,
    rpc_source: str | None = None,
) -> dict:
    net = NETWORKS[chain_key]
    return {
        "key": chain_key,
        "network": net["name"],
        "chain_id": net["chain_id"],
        "status": status,
        "block": None,
        "date": None,
        "timestamp": None,
        "type": None,
        "rpc_endpoint": rpc_endpoint,
        "rpc_source": rpc_source,
        "error": error,
    }


async def _query_with_rpc(
    rpc: BlockRpc,
    chain_key: str,
    address: str,
    rpc_endpoint: str,
    rpc_source: str,
) -> dict:
    net = NETWORKS[chain_key]
    try:
        block_num, activity_type = await find_first_activity_block(rpc, address)
    except Exception as exc:
        if is_prune_error(exc):
            raise
        return _error_result(chain_key, str(exc), rpc_endpoint=rpc_endpoint, rpc_source=rpc_source)

    if block_num is None:
        return {
            "key": chain_key,
            "network": net["name"],
            "chain_id": net["chain_id"],
            "status": "no_activity",
            "block": None,
            "date": None,
            "timestamp": None,
            "type": None,
            "rpc_endpoint": rpc_endpoint,
            "rpc_source": rpc_source,
            "error": None,
        }

    try:
        timestamp = await rpc.get_block_timestamp(block_num)
        date_str = utc_iso_from_timestamp(timestamp)
    except Exception:
        timestamp = None
        date_str = None

    return {
        "key": chain_key,
        "network": net["name"],
        "chain_id": net["chain_id"],
        "status": "success",
        "block": block_num,
        "date": date_str,
        "timestamp": timestamp,
        "type": activity_type,
        "rpc_endpoint": rpc_endpoint,
        "rpc_source": rpc_source,
        "error": None,
    }


async def query_single_chain_origin(
    client: httpx.AsyncClient,
    chain_key: str,
    address: str,
    alchemy_subdomains: dict[int, str | None],
    alchemy_key: str | None,
) -> dict:
    net = NETWORKS[chain_key]

    for rpc_url in net["rpcs"]:
        try:
            rpc = PublicRpc(client, rpc_url)
            return await _query_with_rpc(rpc, chain_key, address, rpc_url, "public_rpc")
        except Exception:
            continue

    subdomain = alchemy_subdomains.get(net["chain_id"])
    if subdomain and alchemy_key:
        try:
            rpc = AlchemyRpc(client, subdomain, alchemy_key)
            endpoint = mask_alchemy_endpoint(subdomain)
            return await _query_with_rpc(rpc, chain_key, address, endpoint, "alchemy")
        except Exception as exc:
            return _error_result(
                chain_key,
                f"Alchemy fallback failed: {exc}",
                rpc_source="alchemy",
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


async def query_all_chains_origin(
    address: str,
    alchemy_subdomains: dict[int, str | None],
    alchemy_key: str | None,
) -> list[dict]:
    async with httpx.AsyncClient() as client:
        tasks = [
            query_single_chain_origin(
                client,
                chain_key,
                address,
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
