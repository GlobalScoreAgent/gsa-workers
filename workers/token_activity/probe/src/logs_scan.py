"""Batch eth_getLogs Transfer probe (ERC-20/721) — activity detection only."""

from __future__ import annotations

import logging
import re
from typing import Any

from rpc import (
    RpcClient,
    RpcError,
    address_to_topic,
    hex_to_int,
    int_to_hex,
    is_logs_query_too_heavy,
    topic_to_address,
)

logger = logging.getLogger("wallet_token_activity_scan")

TRANSFER_TOPIC0 = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)
_MAX_BLOCK_RANGE_RE = re.compile(
    r"max(?:imum)?\s+block\s+range[:\s]+(\d+)",
    re.IGNORECASE,
)


def classify_transfer(topics: list[str]) -> str | None:
    if not topics or topics[0].lower() != TRANSFER_TOPIC0:
        return None
    n = len(topics)
    if n == 3:
        return "erc20"
    if n >= 4:
        return "erc721"
    return None


def lookback_blocks(days: int, block_time_sec: float) -> int:
    if days <= 0:
        return 0
    bt = max(block_time_sec, 0.1)
    return int((days * 86400) / bt)


async def probe_wallet_batch(
    rpc: RpcClient,
    *,
    wallets: list[dict[str, Any]],
    block_time_sec: float,
    catchup_max_days: int,
    chunk_blocks: int,
    chunk_min: int,
    chunk_max: int,
) -> tuple[set[int], int]:
    """
    Detect wallets with any ERC-20/721 Transfer in [last_scanned+1, tip]
    (floored to tip - catchup_max_days).

    Returns (active_wallet_ids, to_block_scanned).
    """
    if not wallets:
        return set(), 0

    tip = await rpc.eth_block_number()
    max_lookback = lookback_blocks(catchup_max_days, block_time_sec)
    floor = max(0, tip - max_lookback) if max_lookback > 0 else 0

    from_candidates: list[int] = []
    for w in wallets:
        last = w.get("token_activity_last_scanned_block")
        if last is None:
            from_candidates.append(floor)
        else:
            from_candidates.append(max(floor, int(last) + 1))

    from_block = min(from_candidates)
    to_block = tip

    if from_block > to_block:
        logger.info(
            "Batch already caught up tip=%s from=%s wallets=%s",
            tip,
            from_block,
            len(wallets),
        )
        return set(), tip

    addr_to_wallet: dict[str, dict[str, Any]] = {
        str(w["address"]).lower(): w for w in wallets
    }
    topics_addrs = [address_to_topic(a) for a in addr_to_wallet]
    pending = {int(w["wallet_id"]) for w in wallets}
    active: set[int] = set()

    chunk = max(chunk_min, min(chunk_blocks, chunk_max))
    effective_max = chunk_max
    cursor = from_block

    while cursor <= to_block:
        if not pending:
            logger.info(
                "All wallets in batch already flagged active; skipping remaining blocks"
            )
            break

        end = min(cursor + chunk - 1, to_block)
        try:
            logs = await _fetch_transfer_logs(
                rpc,
                from_block=cursor,
                to_block=end,
                address_topics=topics_addrs,
            )
        except RpcError as exc:
            if is_logs_query_too_heavy(exc) and chunk > chunk_min:
                msg = str(exc).lower()
                m = _MAX_BLOCK_RANGE_RE.search(str(exc))
                if m:
                    provider_cap = max(chunk_min, int(m.group(1)))
                    chunk = max(chunk_min, min(provider_cap, chunk // 2 if chunk > provider_cap else provider_cap))
                    effective_max = min(effective_max, provider_cap)
                elif "max range: 800" in msg or "-32047" in msg:
                    chunk = max(chunk_min, min(800, chunk // 2 if chunk <= 800 else 800))
                    effective_max = min(effective_max, 800)
                else:
                    chunk = max(chunk_min, chunk // 2)
                logger.warning(
                    "getLogs range/response too heavy [%s,%s]; shrink chunk -> %s "
                    "(max=%s) (%s)",
                    cursor,
                    end,
                    chunk,
                    effective_max,
                    exc,
                )
                continue
            raise

        for log in logs:
            for wid in _wallet_ids_from_log(log, addr_to_wallet):
                if wid in pending:
                    active.add(wid)
                    pending.discard(wid)

        if chunk < effective_max:
            chunk = min(effective_max, max(chunk + 1, int(chunk * 1.5)))
        cursor = end + 1

    return active, to_block


async def _fetch_transfer_logs(
    rpc: RpcClient,
    *,
    from_block: int,
    to_block: int,
    address_topics: list[str],
) -> list[dict[str, Any]]:
    base = {
        "fromBlock": int_to_hex(from_block),
        "toBlock": int_to_hex(to_block),
    }
    logs_from = await rpc.eth_get_logs(
        {**base, "topics": [TRANSFER_TOPIC0, address_topics]}
    )
    logs_to = await rpc.eth_get_logs(
        {**base, "topics": [TRANSFER_TOPIC0, None, address_topics]}
    )
    seen: set[tuple[str, int]] = set()
    out: list[dict[str, Any]] = []
    for log in logs_from + logs_to:
        tx = str(log.get("transactionHash") or "").lower()
        idx = hex_to_int(log.get("logIndex") or "0x0")
        key = (tx, idx)
        if key in seen:
            continue
        seen.add(key)
        out.append(log)
    return out


def _wallet_ids_from_log(
    log: dict[str, Any],
    addr_to_wallet: dict[str, dict[str, Any]],
) -> list[int]:
    topics = [str(t).lower() for t in (log.get("topics") or [])]
    if classify_transfer(topics) is None:
        return []

    frm = topic_to_address(topics[1] if len(topics) > 1 else None)
    to = topic_to_address(topics[2] if len(topics) > 2 else None)
    ids: list[int] = []
    for addr in (frm, to):
        if addr and addr in addr_to_wallet:
            ids.append(int(addr_to_wallet[addr]["wallet_id"]))
    return ids
