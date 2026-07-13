"""Alchemy getTokenBalances client for ERC-20 discovery."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("wallet_token_contracts_discovery")


class AlchemyError(RuntimeError):
    """Raised when Alchemy Token API fails."""


def _parse_balance_hex(raw: str | None) -> int:
    if raw is None or raw == "" or raw == "0x":
        return 0
    try:
        return int(raw, 16)
    except ValueError:
        return 0


async def fetch_erc20_contracts_with_balance(
    client: httpx.AsyncClient,
    *,
    subdomain: str,
    api_key: str,
    address: str,
    max_pages: int = 50,
) -> list[str]:
    """
    Return lowercase contract addresses with tokenBalance > 0 via alchemy_getTokenBalances.
    Paginates with pageKey (max 100 tokens per page).
    """
    url = f"https://{subdomain}.g.alchemy.com/v2/{api_key}"
    contracts: list[str] = []
    page_key: str | None = None

    for _ in range(max_pages):
        params: list[Any] = [address, "erc20"]
        if page_key:
            params.append({"pageKey": page_key})

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "alchemy_getTokenBalances",
            "params": params,
        }

        try:
            response = await client.post(url, json=payload, timeout=30.0)
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            raise AlchemyError(f"HTTP error for {subdomain}: {exc}") from exc

        if not isinstance(body, dict):
            raise AlchemyError(f"Invalid JSON-RPC body from {subdomain}")
        if body.get("error"):
            raise AlchemyError(f"Alchemy error on {subdomain}: {body['error']}")

        result = body.get("result")
        if not isinstance(result, dict):
            raise AlchemyError(f"Missing result from {subdomain}")

        token_balances = result.get("tokenBalances") or []
        for item in token_balances:
            if not isinstance(item, dict):
                continue
            if item.get("error"):
                continue
            contract = item.get("contractAddress")
            bal = _parse_balance_hex(item.get("tokenBalance"))
            if not contract or bal <= 0:
                continue
            contracts.append(str(contract).strip().lower())

        page_key = result.get("pageKey")
        if not page_key:
            break
    else:
        logger.warning(
            "Hit max_pages=%s for address=%s subdomain=%s; truncating",
            max_pages,
            address[:10],
            subdomain,
        )

    # Dedupe while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for c in contracts:
        if c not in seen and c.startswith("0x") and len(c) == 42:
            seen.add(c)
            unique.append(c)
    return unique
