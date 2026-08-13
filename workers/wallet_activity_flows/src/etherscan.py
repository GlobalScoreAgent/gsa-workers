"""Etherscan API V2 (Free): ETH, Arb, Polygon, Celo."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from canonical import parse_int, row_dict, synth_unique_id

logger = logging.getLogger("wallet_activity_flows")

BASE = "https://api.etherscan.io/v2/api"
ACTIONS = (
    ("txlist", "external"),
    ("tokentx", "erc20"),
    ("tokennfttx", "erc721"),
    ("token1155tx", "erc1155"),
)
PAGE_SIZE = 1000
MIN_INTERVAL_S = 0.35


class EtherscanClient:
    def __init__(self, client: httpx.AsyncClient, api_key: str) -> None:
        self._client = client
        self._api_key = api_key
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def _throttle(self) -> None:
        async with self._lock:
            loop = asyncio.get_event_loop()
            now = loop.time()
            wait = MIN_INTERVAL_S - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = loop.time()

    async def _get(self, params: dict[str, Any]) -> Any:
        await self._throttle()
        params = {**params, "apikey": self._api_key}
        url = f"{BASE}?{urlencode(params)}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        data = resp.json()
        status = str(data.get("status", ""))
        message = str(data.get("message", ""))
        result = data.get("result")
        if status == "0" and message.lower() in {"no transactions found", "no records found"}:
            return []
        if status == "0":
            raise RuntimeError(f"etherscan error: {message}: {result}")
        if not isinstance(result, list):
            return []
        return result

    async def latest_block(self, evm_chain_id: int) -> int:
        await self._throttle()
        params = {
            "chainid": evm_chain_id,
            "module": "proxy",
            "action": "eth_blockNumber",
            "apikey": self._api_key,
        }
        url = f"{BASE}?{urlencode(params)}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result")
        n = parse_int(result)
        if not n:
            raise RuntimeError(f"etherscan eth_blockNumber: {data}")
        return n

    async def fetch_transfers(
        self,
        *,
        wallet_id: int,
        chain_pk: int,
        evm_chain_id: int,
        address: str,
        from_block: int,
        to_block: int,
        window_start: datetime,
        window_end: datetime,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for action, category in ACTIONS:
            page = 1
            while True:
                raw = await self._get(
                    {
                        "chainid": evm_chain_id,
                        "module": "account",
                        "action": action,
                        "address": address,
                        "startblock": from_block,
                        "endblock": to_block,
                        "page": page,
                        "offset": PAGE_SIZE,
                        "sort": "desc",
                    }
                )
                if not raw:
                    break
                for item in raw:
                    mapped = _map_etherscan(
                        item,
                        category=category,
                        wallet_id=wallet_id,
                        chain_pk=chain_pk,
                        evm_chain_id=evm_chain_id,
                        address=address,
                        window_start=window_start,
                        window_end=window_end,
                    )
                    if mapped is not None:
                        rows.append(mapped)
                if len(raw) < PAGE_SIZE:
                    break
                page += 1
        return rows


def _map_etherscan(
    item: dict[str, Any],
    *,
    category: str,
    wallet_id: int,
    chain_pk: int,
    evm_chain_id: int,
    address: str,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any] | None:
    if str(item.get("isError", "0")) == "1":
        return None
    tx_hash = str(item.get("hash") or "").strip()
    if not tx_hash:
        return None
    ts = parse_int(item.get("timeStamp"))
    block_ts = datetime.fromtimestamp(ts, tz=window_start.tzinfo) if ts else None
    log_index = str(item.get("logIndex") or item.get("transactionIndex") or "0")
    token_id = item.get("tokenID") or item.get("tokenId")
    native_asset = {1: "ETH", 42161: "ETH", 137: "POL", 42220: "CELO"}
    if category == "external":
        decimal = 18
        contract = None
        asset = native_asset.get(evm_chain_id, "native")
        unique = synth_unique_id(tx_hash, category, "0")
        value_raw = parse_int(item.get("value"))
    else:
        decimal = parse_int(item.get("tokenDecimal"))
        contract = item.get("contractAddress")
        asset = item.get("tokenSymbol")
        unique = synth_unique_id(tx_hash, category, log_index)
        value_raw = parse_int(item.get("value") or item.get("tokenValue"))
    return row_dict(
        wallet_id=wallet_id,
        chain_id=chain_pk,
        tx_hash=tx_hash,
        block_number=parse_int(item.get("blockNumber")),
        block_timestamp=block_ts,
        from_address=item.get("from"),
        to_address=item.get("to"),
        category=category,
        asset=str(asset) if asset else None,
        contract_address=contract,
        token_decimal=decimal,
        token_id=str(token_id) if token_id not in (None, "") else None,
        value_raw=value_raw,
        unique_id=unique,
        provider="etherscan",
        window_start=window_start,
        window_end=window_end,
        wallet_address=address,
    )
