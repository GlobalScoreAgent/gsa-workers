"""Alchemy alchemy_getAssetTransfers (Base/Gnosis key_1, BSC key_2)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from canonical import parse_int, row_dict, synth_unique_id

logger = logging.getLogger("wallet_activity_flows")

CATEGORIES = ["external", "erc20", "erc721", "erc1155"]


class AlchemyClient:
    def __init__(self, client: httpx.AsyncClient, api_key: str) -> None:
        self._client = client
        self._api_key = api_key

    def _url(self, subdomain: str) -> str:
        return f"https://{subdomain}.g.alchemy.com/v2/{self._api_key}"

    async def _rpc(self, subdomain: str, method: str, params: list[Any]) -> Any:
        resp = await self._client.post(
            self._url(subdomain),
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("error"):
            raise RuntimeError(f"alchemy {method}: {payload['error']}")
        return payload.get("result")

    async def latest_block(self, subdomain: str) -> int:
        result = await self._rpc(subdomain, "eth_blockNumber", [])
        return parse_int(result) or 0

    async def get_block_timestamp(self, subdomain: str, block_number: int) -> datetime | None:
        result = await self._rpc(
            subdomain,
            "eth_getBlockByNumber",
            [hex(block_number), False],
        )
        if not isinstance(result, dict):
            return None
        ts = parse_int(result.get("timestamp"))
        if ts is None:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    async def fetch_transfers(
        self,
        *,
        wallet_id: int,
        chain_pk: int,
        address: str,
        subdomain: str,
        from_block: int,
        window_start: datetime,
        window_end: datetime,
        with_metadata: bool,
    ) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for direction in ("fromAddress", "toAddress"):
            page_key: str | None = None
            while True:
                params: dict[str, Any] = {
                    "fromBlock": hex(from_block),
                    "toBlock": "latest",
                    direction: address,
                    "category": CATEGORIES,
                    "excludeZeroValue": False,
                    "maxCount": "0x3e8",
                    "order": "desc",
                    "withMetadata": with_metadata,
                }
                if page_key:
                    params["pageKey"] = page_key
                result = await self._rpc(subdomain, "alchemy_getAssetTransfers", [params])
                if not isinstance(result, dict):
                    break
                for item in result.get("transfers") or []:
                    mapped = _map_alchemy(
                        item,
                        wallet_id=wallet_id,
                        chain_pk=chain_pk,
                        address=address,
                        window_start=window_start,
                        window_end=window_end,
                    )
                    if mapped is not None:
                        seen[mapped["unique_id"]] = mapped
                page_key = result.get("pageKey")
                if not page_key:
                    break
        return list(seen.values())


def _map_alchemy(
    item: dict[str, Any],
    *,
    wallet_id: int,
    chain_pk: int,
    address: str,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any] | None:
    tx_hash = str(item.get("hash") or "").strip()
    category = str(item.get("category") or "").strip()
    if not tx_hash or category not in CATEGORIES:
        return None
    raw = item.get("rawContract") or {}
    unique = str(item.get("uniqueId") or "").strip() or synth_unique_id(
        tx_hash, category, str(item.get("erc721TokenId") or item.get("tokenId") or "0")
    )
    meta = item.get("metadata") or {}
    block_ts = None
    raw_ts = meta.get("blockTimestamp")
    if raw_ts:
        try:
            block_ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        except ValueError:
            block_ts = None
    token_id = item.get("erc721TokenId") or item.get("tokenId")
    if isinstance(token_id, str) and token_id.startswith("0x"):
        parsed = parse_int(token_id)
        token_id = str(parsed) if parsed is not None else token_id
    return row_dict(
        wallet_id=wallet_id,
        chain_id=chain_pk,
        tx_hash=tx_hash,
        block_number=parse_int(item.get("blockNum")),
        block_timestamp=block_ts,
        from_address=item.get("from"),
        to_address=item.get("to"),
        category=category,
        asset=item.get("asset"),
        contract_address=raw.get("address"),
        token_decimal=parse_int(raw.get("decimal")),
        token_id=str(token_id) if token_id not in (None, "") else None,
        value_raw=parse_int(raw.get("value")),
        unique_id=unique,
        provider="alchemy",
        window_start=window_start,
        window_end=window_end,
        wallet_address=address,
    )
