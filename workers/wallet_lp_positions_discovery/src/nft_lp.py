"""Step 1: UniV3 / Pancake NFT LP positions via Alchemy eth_call."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from eth_utils import to_checksum_address

from networks import FACTORY_FALLBACK, NFPM_BY_CHAIN
from rpc import encode_call, eth_call, multicall3, parse_uint
from univ3_math import amounts_for_liquidity

logger = logging.getLogger("wallet_lp_positions_discovery")

POSITIONS_TYPES = [
    "uint96",
    "address",
    "address",
    "address",
    "uint24",
    "int24",
    "int24",
    "uint128",
    "uint256",
    "uint256",
    "uint128",
    "uint128",
]


async def _call_decode(
    client: httpx.AsyncClient,
    url: str,
    to: str,
    sig: str,
    arg_types: list[str],
    args: list[Any],
    out_types: list[str],
) -> tuple[Any, ...] | None:
    from rpc import decode_result

    data = encode_call(sig, arg_types, args)
    raw = await eth_call(client, url, to, data)
    if not raw:
        return None
    return decode_result(out_types, raw)


async def extract_nft_positions(
    client: httpx.AsyncClient,
    *,
    url: str,
    wallet_address: str,
    chain_id: int,
) -> list[dict[str, Any]]:
    managers = NFPM_BY_CHAIN.get(chain_id) or {}
    if not managers:
        return []

    wallet = to_checksum_address(wallet_address)
    out: list[dict[str, Any]] = []

    for protocol, nfpm in managers.items():
        nfpm_cs = to_checksum_address(nfpm)
        try:
            bal_row = await _call_decode(
                client,
                url,
                nfpm_cs,
                "balanceOf(address)",
                ["address"],
                [wallet],
                ["uint256"],
            )
        except Exception as exc:
            logger.warning(
                "NFPM balanceOf failed chain=%s protocol=%s: %s",
                chain_id,
                protocol,
                exc,
            )
            continue
        if not bal_row:
            continue
        balance = int(bal_row[0])
        if balance <= 0:
            continue
        if balance > 200:
            logger.warning(
                "Truncating NFT scan chain=%s protocol=%s balance=%s to 200",
                chain_id,
                protocol,
                balance,
            )
            balance = 200

        token_calls: list[tuple[str, str]] = []
        for i in range(balance):
            token_calls.append(
                (
                    nfpm_cs,
                    encode_call(
                        "tokenOfOwnerByIndex(address,uint256)",
                        ["address", "uint256"],
                        [wallet, i],
                    ),
                )
            )
        try:
            token_results = await multicall3(client, url, token_calls)
        except Exception as exc:
            logger.warning(
                "tokenOfOwnerByIndex multicall failed chain=%s: %s",
                chain_id,
                exc,
            )
            continue

        token_ids: list[int] = []
        for success, data in token_results:
            if success and data:
                token_ids.append(parse_uint(data))

        if not token_ids:
            continue

        pos_calls = [
            (
                nfpm_cs,
                encode_call("positions(uint256)", ["uint256"], [tid]),
            )
            for tid in token_ids
        ]
        try:
            pos_results = await multicall3(client, url, pos_calls)
        except Exception as exc:
            logger.warning("positions multicall failed chain=%s: %s", chain_id, exc)
            continue

        factory = await _resolve_factory(client, url, nfpm_cs, protocol, chain_id)

        for tid, (success, data) in zip(token_ids, pos_results, strict=False):
            if not success or not data:
                continue
            try:
                from rpc import decode_result

                decoded = decode_result(POSITIONS_TYPES, "0x" + data.hex())
            except Exception as exc:
                logger.warning("decode positions token_id=%s: %s", tid, exc)
                continue

            token0 = str(decoded[2]).lower()
            token1 = str(decoded[3]).lower()
            fee = int(decoded[4])
            tick_lower = int(decoded[5])
            tick_upper = int(decoded[6])
            liquidity = int(decoded[7])
            if liquidity <= 0:
                continue

            pool = await _resolve_pool(
                client, url, factory, token0, token1, fee
            )
            if not pool:
                logger.warning(
                    "No pool for %s/%s fee=%s chain=%s",
                    token0,
                    token1,
                    fee,
                    chain_id,
                )
                continue

            sqrt_price = await _slot0_sqrt(client, url, pool)
            if sqrt_price is None:
                continue

            amount0, amount1 = amounts_for_liquidity(
                sqrt_price, tick_lower, tick_upper, liquidity
            )
            dec0 = await _decimals(client, url, token0)
            dec1 = await _decimals(client, url, token1)

            group_id = f"{protocol}:{nfpm.lower()}:{tid}"
            out.append(
                {
                    "protocol": protocol,
                    "protocol_module": "nft_manager",
                    "position_kind": "nft",
                    "nft_manager_address": nfpm.lower(),
                    "token_id": str(tid),
                    "pool_address": pool.lower(),
                    "token0_address": token0,
                    "token1_address": token1,
                    "fee": fee,
                    "tick_lower": tick_lower,
                    "tick_upper": tick_upper,
                    "liquidity": str(liquidity),
                    "amount0_raw": str(amount0),
                    "amount0_float": amount0 / (10**dec0) if dec0 is not None else None,
                    "amount1_raw": str(amount1),
                    "amount1_float": amount1 / (10**dec1) if dec1 is not None else None,
                    "decimals0": dec0,
                    "decimals1": dec1,
                    "group_id": group_id,
                    "position_type": "locked",
                }
            )

    return out


async def _resolve_factory(
    client: httpx.AsyncClient,
    url: str,
    nfpm: str,
    protocol: str,
    chain_id: int,
) -> str:
    try:
        row = await _call_decode(
            client, url, nfpm, "factory()", [], [], ["address"]
        )
        if row and row[0]:
            return to_checksum_address(row[0])
    except Exception:
        pass
    fallback = FACTORY_FALLBACK.get(f"{protocol}:{chain_id}")
    if not fallback:
        raise RuntimeError(f"No factory for {protocol} chain {chain_id}")
    return to_checksum_address(fallback)


async def _resolve_pool(
    client: httpx.AsyncClient,
    url: str,
    factory: str,
    token0: str,
    token1: str,
    fee: int,
) -> str | None:
    try:
        row = await _call_decode(
            client,
            url,
            factory,
            "getPool(address,address,uint24)",
            ["address", "address", "uint24"],
            [to_checksum_address(token0), to_checksum_address(token1), fee],
            ["address"],
        )
        if not row:
            return None
        addr = str(row[0]).lower()
        if addr in ("", "0x0000000000000000000000000000000000000000"):
            return None
        return addr
    except Exception as exc:
        logger.warning("getPool failed: %s", exc)
        return None


async def _slot0_sqrt(
    client: httpx.AsyncClient,
    url: str,
    pool: str,
) -> int | None:
    try:
        # slot0 returns many fields; first is sqrtPriceX96
        data = encode_call("slot0()", [], [])
        raw = await eth_call(client, url, pool, data)
        if not raw:
            return None
        from rpc import decode_result

        # Uniswap V3: sqrtPriceX96, tick, ...
        decoded = decode_result(
            ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"],
            raw,
        )
        return int(decoded[0])
    except Exception as exc:
        logger.warning("slot0 failed pool=%s: %s", pool, exc)
        return None


_decimals_cache: dict[str, int] = {}


async def _decimals(
    client: httpx.AsyncClient,
    url: str,
    token: str,
) -> int | None:
    key = token.lower()
    if key in _decimals_cache:
        return _decimals_cache[key]
    try:
        row = await _call_decode(
            client, url, token, "decimals()", [], [], ["uint8"]
        )
        if row is None:
            return 18
        val = int(row[0])
        _decimals_cache[key] = val
        return val
    except Exception:
        return 18
