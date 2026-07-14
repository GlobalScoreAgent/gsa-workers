"""Orchestrate NFT + classic LP extraction and pricing for one wallet+chain."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from classic_lp import extract_classic_positions
from nft_lp import extract_nft_positions
from pricing import (
    apply_prices_to_rows,
    collect_underlying_addresses,
    fetch_defillama_prices,
    llama_coin_key,
)
from rpc import alchemy_url

logger = logging.getLogger("wallet_lp_positions_discovery")


async def extract_raw_lp_positions(
    client: httpx.AsyncClient,
    *,
    wallet_address: str,
    chain_id: int,
    subdomain: str,
    alchemy_key: str,
    classic_pools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    url = alchemy_url(subdomain, alchemy_key)
    rows: list[dict[str, Any]] = []

    try:
        rows.extend(
            await extract_nft_positions(
                client,
                url=url,
                wallet_address=wallet_address,
                chain_id=chain_id,
            )
        )
    except Exception as exc:
        logger.warning(
            "NFT step failed wallet=%s chain=%s: %s",
            wallet_address,
            chain_id,
            exc,
        )

    try:
        rows.extend(
            await extract_classic_positions(
                client,
                url=url,
                wallet_address=wallet_address,
                pools=classic_pools,
            )
        )
    except Exception as exc:
        logger.warning(
            "Classic LP step failed wallet=%s chain=%s: %s",
            wallet_address,
            chain_id,
            exc,
        )

    return rows


async def price_lp_positions(
    client: httpx.AsyncClient,
    rows: list[dict[str, Any]],
    *,
    chain_id: int,
    db_prices: dict[str, float],
) -> list[dict[str, Any]]:
    underlyings = collect_underlying_addresses(rows)
    llama_keys = [
        k
        for a in underlyings
        if (k := llama_coin_key(chain_id, a)) is not None
    ]
    llama_prices = await fetch_defillama_prices(client, llama_keys)
    return apply_prices_to_rows(
        rows,
        chain_id=chain_id,
        llama_prices=llama_prices,
        db_prices=db_prices,
    )
