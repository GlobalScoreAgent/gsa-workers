"""Price LP underlyings via DeFiLlama then wallets.token_prices fallback."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from networks import CHAIN_META

logger = logging.getLogger("wallet_lp_positions_discovery")


async def fetch_defillama_prices(
    client: httpx.AsyncClient,
    coins_list: list[str],
) -> dict[str, float]:
    if not coins_list:
        return {}
    prices: dict[str, float] = {}
    for i in range(0, len(coins_list), 50):
        chunk = coins_list[i : i + 50]
        coins_query = ",".join(chunk)
        url = f"https://coins.llama.fi/prices/current/{coins_query}"
        try:
            response = await client.get(
                url,
                headers={"User-Agent": "gsa-workers/wallet_lp_positions_discovery"},
                timeout=15.0,
            )
            response.raise_for_status()
            data = response.json()
            for coin_key, info in (data.get("coins") or {}).items():
                try:
                    prices[str(coin_key).lower()] = float(info.get("price") or 0.0)
                except (TypeError, ValueError):
                    continue
        except Exception as exc:
            logger.warning("DeFiLlama prices chunk failed: %s", exc)
    return prices


def llama_coin_key(chain_id: int, contract: str) -> str | None:
    meta = CHAIN_META.get(chain_id)
    if not meta:
        return None
    return f"{meta['llama_chain']}:{contract.lower()}"


def apply_prices_to_rows(
    rows: list[dict[str, Any]],
    *,
    chain_id: int,
    llama_prices: dict[str, float],
    db_prices: dict[str, float],
) -> list[dict[str, Any]]:
    priced: list[dict[str, Any]] = []
    for row in rows:
        token0 = (row.get("token0_address") or "").lower()
        token1 = (row.get("token1_address") or "").lower()
        p0 = _lookup_price(chain_id, token0, llama_prices, db_prices)
        p1 = _lookup_price(chain_id, token1, llama_prices, db_prices)

        a0 = row.get("amount0_float")
        a1 = row.get("amount1_float")
        value = None
        has_err = False
        if a0 is not None and p0 and p0 > 0:
            value = (value or 0.0) + float(a0) * p0
        elif a0 is not None and float(a0) > 0:
            has_err = True
        if a1 is not None and p1 and p1 > 0:
            value = (value or 0.0) + float(a1) * p1
        elif a1 is not None and float(a1) > 0:
            has_err = True
        if a0 is None and a1 is None:
            has_err = True

        if has_err:
            quality, reason = "unpriced", "no_defillama_or_token_prices"
        elif value is not None and value > 0:
            quality, reason = "priced", None
        else:
            quality, reason = "unpriced", "zero_or_missing_amounts"

        out = {
            k: v
            for k, v in row.items()
            if k not in ("decimals0", "decimals1")
        }
        out["price_usd_token0"] = p0
        out["price_usd_token1"] = p1
        out["value_usd"] = value
        out["has_price_error"] = has_err
        out["token_quality"] = quality
        out["quality_reason"] = reason
        out["source"] = "lp_discovery"
        priced.append(out)
    return priced


def _lookup_price(
    chain_id: int,
    contract: str,
    llama_prices: dict[str, float],
    db_prices: dict[str, float],
) -> float | None:
    if not contract:
        return None
    key = llama_coin_key(chain_id, contract)
    if key:
        p = llama_prices.get(key.lower())
        if p and p > 0:
            return p
    p2 = db_prices.get(contract.lower())
    if p2 and p2 > 0:
        return p2
    return None


def collect_underlying_addresses(rows: list[dict[str, Any]]) -> list[str]:
    addrs: set[str] = set()
    for row in rows:
        for k in ("token0_address", "token1_address"):
            v = row.get(k)
            if v:
                addrs.add(str(v).lower())
    return sorted(addrs)
