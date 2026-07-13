"""CoinGecko simple token price client."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger("token_prices_import")

CG_TOKEN_PRICE_URL = "https://api.coingecko.com/api/v3/simple/token_price/{platform}"
DEFAULT_BATCH_SIZE = 100
DEFAULT_REQUEST_DELAY_SECONDS = 0.7


def fetch_coingecko_prices(
    client: httpx.Client,
    *,
    api_key: str,
    platform: str,
    contracts: list[str],
    batch_size: int = DEFAULT_BATCH_SIZE,
    request_delay_seconds: float = DEFAULT_REQUEST_DELAY_SECONDS,
) -> dict[str, float]:
    """Return lower(contract) -> usd price for a CoinGecko platform slug."""
    out: dict[str, float] = {}
    if not contracts or not platform or not api_key:
        return out

    for i in range(0, len(contracts), batch_size):
        if i > 0 and request_delay_seconds > 0:
            time.sleep(request_delay_seconds)
        chunk = contracts[i : i + batch_size]
        params: dict[str, Any] = {
            "contract_addresses": ",".join(chunk),
            "vs_currencies": "usd",
            "x_cg_demo_api_key": api_key,
        }
        url = CG_TOKEN_PRICE_URL.format(platform=platform)
        try:
            response = client.get(url, params=params, timeout=30.0)
            if response.status_code == 429:
                logger.warning("CoinGecko 429; sleeping 5s")
                time.sleep(5.0)
                response = client.get(url, params=params, timeout=30.0)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning(
                "CoinGecko batch failed platform=%s n=%s: %s",
                platform,
                len(chunk),
                exc,
            )
            continue

        if not isinstance(data, dict):
            continue
        for addr, info in data.items():
            if not isinstance(info, dict):
                continue
            try:
                price = float(info.get("usd") or 0.0)
            except (TypeError, ValueError):
                continue
            if price > 0:
                out[str(addr).strip().lower()] = price

    return out
