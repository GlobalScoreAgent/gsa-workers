"""Step 2: classic ERC-20 LP + gauge balances from wallets.lp_pools."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from eth_utils import to_checksum_address

from rpc import encode_call, eth_call, multicall3, parse_uint

logger = logging.getLogger("wallet_lp_positions_discovery")

ZERO = "0x0000000000000000000000000000000000000000"


async def extract_classic_positions(
    client: httpx.AsyncClient,
    *,
    url: str,
    wallet_address: str,
    pools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not pools:
        return []

    wallet = to_checksum_address(wallet_address)
    calls: list[tuple[str, str]] = []
    call_meta: list[tuple[dict[str, Any], str]] = []  # pool row, "lp"|"staked"

    for pool in pools:
        pool_addr = to_checksum_address(str(pool["pool_address"]))
        calls.append(
            (pool_addr, encode_call("balanceOf(address)", ["address"], [wallet]))
        )
        call_meta.append((pool, "lp"))

        gauge = pool.get("gauge_address")
        if gauge and str(gauge).lower() not in ("", ZERO):
            gauge_cs = to_checksum_address(str(gauge))
            calls.append(
                (
                    gauge_cs,
                    encode_call("balanceOf(address)", ["address"], [wallet]),
                )
            )
            call_meta.append((pool, "staked"))

    try:
        results = await multicall3(client, url, calls)
    except Exception as exc:
        logger.warning("classic LP multicall failed: %s", exc)
        return []

    balances: dict[str, dict[str, int]] = {}
    for (pool, kind), (success, data) in zip(call_meta, results, strict=False):
        key = str(pool["pool_address"]).lower()
        balances.setdefault(key, {"lp": 0, "staked": 0, "pool": pool})
        if success and data:
            balances[key][kind] = parse_uint(data)

    out: list[dict[str, Any]] = []
    for key, entry in balances.items():
        pool = entry["pool"]
        protocol = str(pool.get("protocol") or "classic")
        token0 = str(pool["token0_address"]).lower()
        token1 = str(pool["token1_address"]).lower()
        pool_addr = key

        for kind, bal_key, position_kind, position_type in (
            ("lp", "lp", "classic_lp", "locked"),
            ("staked", "staked", "classic_staked", "staked"),
        ):
            bal = int(entry.get(bal_key) or 0)
            if bal <= 0:
                continue
            amount0, amount1, dec0, dec1 = await _share_of_reserves(
                client, url, pool_addr, bal
            )
            group_id = f"{protocol}:{pool_addr}:{kind}"
            out.append(
                {
                    "protocol": protocol,
                    "protocol_module": "classic_pool",
                    "position_kind": position_kind,
                    "nft_manager_address": None,
                    "token_id": None,
                    "pool_address": pool_addr,
                    "token0_address": token0,
                    "token1_address": token1,
                    "fee": None,
                    "tick_lower": None,
                    "tick_upper": None,
                    "liquidity": str(bal),
                    "amount0_raw": str(amount0) if amount0 is not None else None,
                    "amount0_float": (
                        amount0 / (10**dec0)
                        if amount0 is not None and dec0 is not None
                        else None
                    ),
                    "amount1_raw": str(amount1) if amount1 is not None else None,
                    "amount1_float": (
                        amount1 / (10**dec1)
                        if amount1 is not None and dec1 is not None
                        else None
                    ),
                    "decimals0": dec0,
                    "decimals1": dec1,
                    "group_id": group_id,
                    "position_type": position_type,
                }
            )
    return out


async def _share_of_reserves(
    client: httpx.AsyncClient,
    url: str,
    pool: str,
    lp_balance: int,
) -> tuple[int | None, int | None, int | None, int | None]:
    """amount0/1 = lp_balance / totalSupply * reserve0/1."""
    from nft_lp import _decimals
    from rpc import decode_result

    try:
        raw_ts = await eth_call(
            client, url, pool, encode_call("totalSupply()", [], [])
        )
        raw_res = await eth_call(
            client, url, pool, encode_call("getReserves()", [], [])
        )
        if not raw_ts or not raw_res:
            return None, None, None, None
        total_supply = int(decode_result(["uint256"], raw_ts)[0])
        # UniswapV2-style: reserve0, reserve1, blockTimestampLast
        reserves = decode_result(["uint112", "uint112", "uint32"], raw_res)
        reserve0 = int(reserves[0])
        reserve1 = int(reserves[1])
        if total_supply <= 0:
            return None, None, None, None
        amount0 = (lp_balance * reserve0) // total_supply
        amount1 = (lp_balance * reserve1) // total_supply

        # token0/token1 decimals from pair
        t0 = await eth_call(client, url, pool, encode_call("token0()", [], []))
        t1 = await eth_call(client, url, pool, encode_call("token1()", [], []))
        dec0 = dec1 = 18
        if t0:
            token0 = "0x" + bytes.fromhex(t0[2:])[-20:].hex()
            dec0 = await _decimals(client, url, token0) or 18
        if t1:
            token1 = "0x" + bytes.fromhex(t1[2:])[-20:].hex()
            dec1 = await _decimals(client, url, token1) or 18
        return amount0, amount1, dec0, dec1
    except Exception as exc:
        logger.warning("share_of_reserves failed pool=%s: %s", pool, exc)
        return None, None, None, None
