"""Smoke: hit each activity-flows provider once. Skips vendors without env keys unless SMOKE_REQUIRE_ALL=1."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from alchemy import AlchemyClient
from ankr import AnkrClient
from etherscan import EtherscanClient
from okx import NATIVE_PATH, TOKEN_PATH, OkxDataClient

SAMPLE = os.environ.get(
    "SMOKE_ADDRESS", "0x000000000000000000000000000000000000dEaD"
)
REQUIRE_ALL = os.environ.get("SMOKE_REQUIRE_ALL", "").strip() in {"1", "true", "TRUE"}


def _need(name: str, value: str) -> str:
    if value:
        return value
    if REQUIRE_ALL:
        raise RuntimeError(f"missing {name}")
    return ""


async def main() -> int:
    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(days=15)
    failed = 0
    skipped = 0

    etherscan_key = _need("ETHERSCAN_API_KEY", os.environ.get("ETHERSCAN_API_KEY", "").strip())
    k1 = _need("ALCHEMY_ACTIVITY_KEY_1", os.environ.get("ALCHEMY_ACTIVITY_KEY_1", "").strip())
    k2 = _need("ALCHEMY_ACTIVITY_KEY_2", os.environ.get("ALCHEMY_ACTIVITY_KEY_2", "").strip())
    ankr_key = _need("ANKR_API_KEY", os.environ.get("ANKR_API_KEY", "").strip())
    okx_key = _need("OKX_API_KEY", os.environ.get("OKX_API_KEY", "").strip())
    okx_secret = _need("OKX_SECRET_KEY", os.environ.get("OKX_SECRET_KEY", "").strip())
    okx_pass = _need("OKX_PASSPHRASE", os.environ.get("OKX_PASSPHRASE", "").strip())

    async with httpx.AsyncClient(timeout=45.0) as client:
        if etherscan_key:
            es = EtherscanClient(client, etherscan_key)
            for chain_id in (1, 42161, 137, 42220):
                try:
                    latest = await es.latest_block(chain_id)
                    raw = await es._get(
                        {
                            "chainid": chain_id,
                            "module": "account",
                            "action": "txlist",
                            "address": SAMPLE,
                            "startblock": max(0, latest - 100),
                            "endblock": latest,
                            "page": 1,
                            "offset": 1,
                            "sort": "desc",
                        }
                    )
                    print(
                        f"etherscan chain={chain_id} latest={latest} txlist={len(raw)} OK"
                    )
                except Exception as exc:
                    failed += 1
                    print(f"etherscan chain={chain_id} FAIL {exc}")
        else:
            skipped += 1
            print("SKIP etherscan (no ETHERSCAN_API_KEY)")

        if k1:
            alk = AlchemyClient(client, k1)
            for sub, with_meta in (("base-mainnet", True), ("gnosis-mainnet", False)):
                try:
                    latest = await alk.latest_block(sub)
                    print(f"alchemy_k1 {sub} latest={latest} OK")
                    if sub == "gnosis-mainnet":
                        ts = await alk.get_block_timestamp(sub, latest)
                        print(f"alchemy_k1 gnosis getBlockByNumber ts={ts} OK")
                    rows = await alk.fetch_transfers(
                        wallet_id=0,
                        chain_pk=0,
                        address=SAMPLE,
                        subdomain=sub,
                        from_block=max(0, latest - 50),
                        window_start=window_start,
                        window_end=window_end,
                        with_metadata=with_meta,
                    )
                    print(f"alchemy_k1 {sub} transfers={len(rows)} OK")
                except Exception as exc:
                    failed += 1
                    print(f"alchemy_k1 {sub} FAIL {exc}")
        else:
            skipped += 1
            print("SKIP alchemy_k1 (no ALCHEMY_ACTIVITY_KEY_1)")

        if k2:
            alk2 = AlchemyClient(client, k2)
            try:
                latest = await alk2.latest_block("bnb-mainnet")
                rows = await alk2.fetch_transfers(
                    wallet_id=0,
                    chain_pk=0,
                    address=SAMPLE,
                    subdomain="bnb-mainnet",
                    from_block=max(0, latest - 50),
                    window_start=window_start,
                    window_end=window_end,
                    with_metadata=True,
                )
                print(f"alchemy_k2 bsc latest={latest} transfers={len(rows)} OK")
            except Exception as exc:
                failed += 1
                print(f"alchemy_k2 bsc FAIL {exc}")
        else:
            skipped += 1
            print("SKIP alchemy_k2 (no ALCHEMY_ACTIVITY_KEY_2)")

        if ankr_key:
            ankr = AnkrClient(client, ankr_key)
            from_ts = int(window_start.timestamp())
            to_ts = int(window_end.timestamp())
            probes = (
                (
                    "ankr_getTransactionsByAddress",
                    {
                        "blockchain": "bsc",
                        "address": SAMPLE,
                        "fromTimestamp": from_ts,
                        "toTimestamp": to_ts,
                        "descOrder": True,
                        "pageSize": 1,
                    },
                    ("transactions", "transfers"),
                ),
                (
                    "ankr_getTokenTransfers",
                    {
                        "blockchain": "bsc",
                        "address": [SAMPLE],
                        "fromTimestamp": from_ts,
                        "toTimestamp": to_ts,
                        "descOrder": True,
                        "pageSize": 1,
                    },
                    ("transfers",),
                ),
                (
                    "ankr_getNftTransfers",
                    {
                        "blockchain": "bsc",
                        "address": [SAMPLE],
                        "fromTimestamp": from_ts,
                        "toTimestamp": to_ts,
                        "descOrder": True,
                        "pageSize": 1,
                    },
                    ("transfers", "nfts"),
                ),
            )
            for method, params, keys in probes:
                try:
                    result = await ankr._rpc(method, params)
                    n = 0
                    for key in keys:
                        n += len(result.get(key) or [])
                    print(f"ankr {method} rows={n} OK")
                except Exception as exc:
                    failed += 1
                    print(f"ankr {method} FAIL {exc}")
        else:
            skipped += 1
            print("SKIP ankr (no ANKR_API_KEY)")

        if okx_key and okx_secret and okx_pass:
            okx = OkxDataClient(
                client, api_key=okx_key, secret=okx_secret, passphrase=okx_pass
            )
            try:
                native = await okx._get(
                    NATIVE_PATH,
                    {
                        "chainShortName": "xlayer",
                        "address": SAMPLE,
                        "limit": 1,
                        "page": 1,
                    },
                )
                print(f"okx native list rows={len(native)} OK")
                for proto in ("token_20", "token_721", "token_1155"):
                    token_rows = await okx._get(
                        TOKEN_PATH,
                        {
                            "chainShortName": "xlayer",
                            "address": SAMPLE,
                            "protocolType": proto,
                            "limit": 1,
                            "page": 1,
                        },
                    )
                    print(f"okx {proto} rows={len(token_rows)} OK")
            except Exception as exc:
                failed += 1
                print(f"okx data api FAIL {exc}")
        else:
            skipped += 1
            print("SKIP okx (no OKX HMAC trio)")

    print(f"smoke done failed={failed} skipped={skipped}")
    if REQUIRE_ALL and skipped:
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
