"""Alchemy JSON-RPC for historical wallet origin queries."""

from __future__ import annotations

import httpx

from rpc import (
    RpcError,
    block_to_hex,
    eth_block_number,
    eth_get_balance_at_block,
    eth_get_block_timestamp,
    eth_get_code_at_block,
    eth_get_nonce_at_block,
    hex_to_int,
)


def build_alchemy_rpc_url(subdomain: str, alchemy_key: str) -> str:
    return f"https://{subdomain}.g.alchemy.com/v2/{alchemy_key}"


def mask_alchemy_endpoint(subdomain: str) -> str:
    """Persist endpoint without exposing the API key."""
    return f"https://{subdomain}.g.alchemy.com/v2/***"


class AlchemyRpc:
    def __init__(self, client: httpx.AsyncClient, subdomain: str, alchemy_key: str):
        self._client = client
        self.url = build_alchemy_rpc_url(subdomain, alchemy_key)
        self.subdomain = subdomain

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
