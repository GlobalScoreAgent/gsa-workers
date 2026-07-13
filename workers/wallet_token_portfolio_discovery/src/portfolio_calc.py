"""Fungible portfolio calculation: Alchemy balances + DeFiLlama prices.

Decoupled from the claim loop so a future 15-day updater can reuse this module.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from networks import CHAIN_META

logger = logging.getLogger("wallet_token_portfolio_discovery")

NATIVE_SENTINEL = "native"
CHUNK_SIZE = 80


def _alchemy_url(subdomain: str, api_key: str) -> str:
    return f"https://{subdomain}.g.alchemy.com/v2/{api_key}"


def _parse_hex_int(raw: str | None) -> int:
    if raw is None or raw in ("", "0x", "0x0"):
        return 0
    try:
        return int(raw, 16)
    except ValueError:
        return 0


async def _rpc(
    client: httpx.AsyncClient,
    url: str,
    method: str,
    params: list[Any],
) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    response = await client.post(url, json=payload, timeout=45.0)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"Invalid RPC body for {method}")
    if body.get("error"):
        raise RuntimeError(f"RPC {method} error: {body['error']}")
    return body.get("result")


async def fetch_defillama_prices(
    client: httpx.AsyncClient,
    coins_list: list[str],
) -> dict[str, float]:
    """Fetch USD prices from DeFiLlama. coins like 'ethereum:0x...' or 'coingecko:ethereum'."""
    if not coins_list:
        return {}
    # API limits long URLs; chunk
    prices: dict[str, float] = {}
    for i in range(0, len(coins_list), 50):
        chunk = coins_list[i : i + 50]
        coins_query = ",".join(chunk)
        url = f"https://coins.llama.fi/prices/current/{coins_query}"
        try:
            response = await client.get(
                url,
                headers={"User-Agent": "gsa-workers/wallet_token_portfolio_discovery"},
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


async def fetch_native_balance(
    client: httpx.AsyncClient,
    *,
    url: str,
    address: str,
) -> tuple[int, float]:
    raw = await _rpc(client, url, "eth_getBalance", [address, "latest"])
    wei = _parse_hex_int(raw if isinstance(raw, str) else None)
    return wei, wei / 1e18


async def fetch_token_balances(
    client: httpx.AsyncClient,
    *,
    url: str,
    address: str,
    contracts: list[str],
) -> dict[str, int]:
    """Return raw token balances for known contracts via alchemy_getTokenBalances."""
    out: dict[str, int] = {}
    for i in range(0, len(contracts), CHUNK_SIZE):
        chunk = contracts[i : i + CHUNK_SIZE]
        result = await _rpc(
            client,
            url,
            "alchemy_getTokenBalances",
            [address, chunk],
        )
        if not isinstance(result, dict):
            continue
        for item in result.get("tokenBalances") or []:
            if not isinstance(item, dict) or item.get("error"):
                continue
            contract = str(item.get("contractAddress") or "").strip().lower()
            bal = _parse_hex_int(item.get("tokenBalance"))
            if contract:
                out[contract] = bal
    return out


async def fetch_token_metadata(
    client: httpx.AsyncClient,
    *,
    url: str,
    contract: str,
) -> dict[str, Any]:
    result = await _rpc(client, url, "alchemy_getTokenMetadata", [contract])
    if not isinstance(result, dict):
        return {}
    decimals = result.get("decimals")
    try:
        decimals_i = int(decimals) if decimals is not None else None
    except (TypeError, ValueError):
        decimals_i = None
    symbol = result.get("symbol")
    return {
        "symbol": str(symbol).strip() if symbol else None,
        "decimals": decimals_i,
    }


def _row(
    *,
    contract_address: str,
    symbol: str | None,
    decimals: int | None,
    amount_raw: int,
    amount_float: float,
    price_usd: float | None,
    has_price_error: bool,
    category: str,
) -> dict[str, Any]:
    value_usd = None
    if price_usd is not None and not has_price_error:
        value_usd = amount_float * price_usd
    return {
        "contract_address": contract_address,
        "symbol": symbol,
        "decimals": decimals,
        "amount_raw": str(amount_raw),
        "amount_float": amount_float,
        "price_usd": price_usd,
        "value_usd": value_usd,
        "has_price_error": has_price_error,
        "category": category,
        "source": "portfolio_discovery",
    }


async def calculate_fungible_positions(
    client: httpx.AsyncClient,
    *,
    wallet_address: str,
    chain_id: int,
    subdomain: str,
    alchemy_key: str,
    contracts: list[str],
) -> list[dict[str, Any]]:
    """
    Build position rows for one wallet+chain.
    Always includes a 'native' row when balance >= 0 (including zero).
    ERC-20 rows only when raw balance > 0.
    """
    meta = CHAIN_META.get(chain_id)
    if meta is None:
        raise RuntimeError(f"Unsupported chain_id={chain_id} for portfolio calc")

    url = _alchemy_url(subdomain, alchemy_key)
    address = wallet_address.strip().lower()
    contracts = [c.strip().lower() for c in contracts if c and c.strip().lower().startswith("0x")]

    native_raw, native_float = await fetch_native_balance(client, url=url, address=address)
    balances = await fetch_token_balances(
        client, url=url, address=address, contracts=contracts
    ) if contracts else {}

    positive_contracts = [c for c, bal in balances.items() if bal > 0]

    # Metadata for positive balances (bounded concurrency via sequential chunks)
    metadata: dict[str, dict[str, Any]] = {}
    for contract in positive_contracts:
        try:
            metadata[contract] = await fetch_token_metadata(client, url=url, contract=contract)
        except Exception as exc:
            logger.warning("metadata failed %s: %s", contract[:12], exc)
            metadata[contract] = {}

    llama_keys: list[str] = [meta["native_llama"]]
    llama_chain = meta["llama_chain"]
    for contract in positive_contracts:
        llama_keys.append(f"{llama_chain}:{contract}")

    prices = await fetch_defillama_prices(client, llama_keys)

    rows: list[dict[str, Any]] = []

    native_price_key = meta["native_llama"].lower()
    native_price = prices.get(native_price_key)
    native_has_err = native_price is None or native_price <= 0
    rows.append(
        _row(
            contract_address=NATIVE_SENTINEL,
            symbol=meta["native_symbol"],
            decimals=int(meta["decimals"]),
            amount_raw=native_raw,
            amount_float=native_float,
            price_usd=None if native_has_err else native_price,
            has_price_error=native_has_err,
            category="native",
        )
    )

    for contract in positive_contracts:
        raw = balances.get(contract, 0)
        md = metadata.get(contract) or {}
        decimals = md.get("decimals")
        if decimals is None:
            decimals = 18
        amount_float = raw / (10 ** int(decimals)) if decimals is not None else float(raw)
        price_key = f"{llama_chain}:{contract}".lower()
        price = prices.get(price_key)
        has_err = price is None or price <= 0
        rows.append(
            _row(
                contract_address=contract,
                symbol=md.get("symbol"),
                decimals=int(decimals),
                amount_raw=raw,
                amount_float=amount_float,
                price_usd=None if has_err else price,
                has_price_error=has_err,
                category="erc20",
            )
        )

    return rows
