"""DexScreener price client."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger("token_prices_import")

DEX_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/{addresses}"
DEFAULT_BATCH_SIZE = 30
DEFAULT_REQUEST_DELAY_SECONDS = 0.25


def fetch_dex_prices(
    client: httpx.Client,
    *,
    contracts: list[str],
    dex_chain_id: str,
    min_liquidity_usd: float,
    batch_size: int = DEFAULT_BATCH_SIZE,
    request_delay_seconds: float = DEFAULT_REQUEST_DELAY_SECONDS,
) -> dict[str, dict[str, Any]]:
    """Return contract -> {price_usd, liquidity_usd, symbol} for dex_chain_id."""
    out: dict[str, dict[str, Any]] = {}
    if not contracts or not dex_chain_id:
        return out

    for i in range(0, len(contracts), batch_size):
        if i > 0 and request_delay_seconds > 0:
            time.sleep(request_delay_seconds)
        chunk = contracts[i : i + batch_size]
        url = DEX_TOKENS_URL.format(addresses=",".join(chunk))
        try:
            response = client.get(url, timeout=30.0)
            if response.status_code == 429:
                logger.warning("DexScreener 429; sleeping 2s")
                time.sleep(2.0)
                response = client.get(url, timeout=30.0)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning("DexScreener batch failed (%s contracts): %s", len(chunk), exc)
            continue

        pairs = data.get("pairs") if isinstance(data, dict) else None
        if not isinstance(pairs, list):
            continue

        best: dict[str, dict[str, Any]] = {}
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            if str(pair.get("chainId") or "").lower() != dex_chain_id.lower():
                continue
            base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
            addr = str(base.get("address") or "").strip().lower()
            if not addr:
                continue
            try:
                price = float(pair.get("priceUsd") or 0.0)
            except (TypeError, ValueError):
                continue
            liq_obj = pair.get("liquidity") if isinstance(pair.get("liquidity"), dict) else {}
            try:
                liq = float(liq_obj.get("usd") or 0.0)
            except (TypeError, ValueError):
                liq = 0.0
            if price <= 0 or liq < min_liquidity_usd:
                continue
            prev = best.get(addr)
            if prev is None or liq > float(prev.get("liquidity_usd") or 0.0):
                best[addr] = {
                    "price_usd": price,
                    "liquidity_usd": liq,
                    "symbol": str(base.get("symbol") or "").strip() or None,
                }
        out.update(best)

    return out
