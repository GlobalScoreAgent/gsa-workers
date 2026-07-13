"""CoinGecko simple token price client."""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

import httpx

logger = logging.getLogger("token_prices_import")

DEFAULT_BATCH_SIZE = 100
DEFAULT_REQUEST_DELAY_SECONDS = 0.7

ApiPlan = Literal["demo", "pro"]

_PLAN_CONFIG: dict[str, dict[str, str]] = {
    "demo": {
        "base_url": "https://api.coingecko.com/api/v3",
        "header": "x-cg-demo-api-key",
    },
    "pro": {
        "base_url": "https://pro-api.coingecko.com/api/v3",
        "header": "x-cg-pro-api-key",
    },
}


class CoinGeckoAuthError(RuntimeError):
    """Raised when CoinGecko rejects the API key (e.g. error_code 10002)."""


def fetch_coingecko_prices(
    client: httpx.Client,
    *,
    api_key: str,
    platform: str,
    contracts: list[str],
    api_plan: ApiPlan = "demo",
    batch_size: int = DEFAULT_BATCH_SIZE,
    request_delay_seconds: float = DEFAULT_REQUEST_DELAY_SECONDS,
) -> dict[str, float]:
    """Return lower(contract) -> usd price for a CoinGecko platform slug."""
    out: dict[str, float] = {}
    if not contracts or not platform or not api_key:
        return out

    plan = (api_plan or "demo").strip().lower()
    if plan not in _PLAN_CONFIG:
        raise ValueError(f"COINGECKO_API_PLAN must be demo|pro, got {api_plan!r}")
    cfg = _PLAN_CONFIG[plan]
    headers = {cfg["header"]: api_key}

    for i in range(0, len(contracts), batch_size):
        if i > 0 and request_delay_seconds > 0:
            time.sleep(request_delay_seconds)
        chunk = contracts[i : i + batch_size]
        params: dict[str, Any] = {
            "contract_addresses": ",".join(chunk),
            "vs_currencies": "usd",
        }
        url = f"{cfg['base_url']}/simple/token_price/{platform}"
        try:
            response = client.get(url, params=params, headers=headers, timeout=30.0)
            if response.status_code == 429:
                logger.warning("CoinGecko 429; sleeping 5s")
                time.sleep(5.0)
                response = client.get(url, params=params, headers=headers, timeout=30.0)
            data = response.json() if response.content else {}
            if isinstance(data, dict):
                status = data.get("status")
                if isinstance(status, dict) and status.get("error_code") is not None:
                    code = status.get("error_code")
                    msg = status.get("error_message") or status.get("error_code")
                    logger.error(
                        "CoinGecko API error plan=%s platform=%s code=%s msg=%s",
                        plan,
                        platform,
                        code,
                        msg,
                    )
                    if int(code) in (10002, 10010, 10011):
                        raise CoinGeckoAuthError(f"CoinGecko auth failed ({code}): {msg}")
                    continue
            response.raise_for_status()
        except CoinGeckoAuthError:
            raise
        except Exception as exc:
            logger.warning(
                "CoinGecko batch failed plan=%s platform=%s n=%s: %s",
                plan,
                platform,
                len(chunk),
                exc,
            )
            continue

        if not isinstance(data, dict):
            continue
        for addr, info in data.items():
            if addr == "status" or not isinstance(info, dict):
                continue
            try:
                price = float(info.get("usd") or 0.0)
            except (TypeError, ValueError):
                continue
            if price > 0:
                out[str(addr).strip().lower()] = price

    return out
