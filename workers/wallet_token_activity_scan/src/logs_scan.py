"""Batch eth_getLogs Transfer scan + ERC-20/721 classification."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from rpc import (
    RpcClient,
    RpcError,
    address_to_topic,
    hex_to_int,
    int_to_hex,
    is_result_too_large,
    topic_to_address,
)

logger = logging.getLogger("wallet_token_activity_scan")

TRANSFER_TOPIC0 = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)
SOURCE = "rpc_logs_activity"


def classify_transfer(topics: list[str]) -> str | None:
    if not topics or topics[0].lower() != TRANSFER_TOPIC0:
        return None
    n = len(topics)
    if n == 3:
        return "erc20"
    if n >= 4:
        return "erc721"
    return None


def _direction(wallet: str, frm: str | None, to: str | None) -> str:
    w = wallet.lower()
    f = (frm or "").lower()
    t = (to or "").lower()
    if f == w and t == w:
        return "self"
    if t == w:
        return "incoming"
    if f == w:
        return "outgoing"
    return "incoming"


def lookback_blocks(days: int, block_time_sec: float) -> int:
    if days <= 0:
        return 0
    bt = max(block_time_sec, 0.1)
    return int((days * 86400) / bt)


async def scan_wallet_batch(
    rpc: RpcClient,
    *,
    wallets: list[dict[str, Any]],
    chain_pk: int,
    block_time_sec: float,
    catchup_max_days: int,
    chunk_blocks: int,
    chunk_min: int,
    chunk_max: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, str]], int]:
    """
    Scan Transfer logs for a batch of wallets on one chain.

    Returns (transfer_rows, erc20_by_wallet, nft_by_wallet_rows, to_block_scanned).

    erc20_by_wallet / nft rows are flat lists tagged with wallet_id for grouping in job.
    """
    if not wallets:
        return [], [], [], 0

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
        return [], [], [], tip

    addr_to_wallet: dict[str, dict[str, Any]] = {
        str(w["address"]).lower(): w for w in wallets
    }
    topics_addrs = [address_to_topic(a) for a in addr_to_wallet]

    transfers: list[dict[str, Any]] = []
    erc20: dict[tuple[int, str], dict[str, Any]] = {}
    nfts: dict[tuple[int, str], dict[str, Any]] = {}

    chunk = max(chunk_min, min(chunk_blocks, chunk_max))
    cursor = from_block

    while cursor <= to_block:
        end = min(cursor + chunk - 1, to_block)
        try:
            logs = await _fetch_transfer_logs(
                rpc,
                from_block=cursor,
                to_block=end,
                address_topics=topics_addrs,
            )
        except RpcError as exc:
            if is_result_too_large(exc) and chunk > chunk_min:
                chunk = max(chunk_min, chunk // 2)
                logger.warning(
                    "getLogs too large [%s,%s]; shrink chunk -> %s",
                    cursor,
                    end,
                    chunk,
                )
                continue
            raise

        for log in logs:
            for row, std in _expand_log(log, addr_to_wallet, chain_pk):
                transfers.append(row)
                key = (int(row["wallet_id"]), row["contract_address"])
                if std == "erc20":
                    erc20[key] = {
                        "wallet_id": row["wallet_id"],
                        "contract_address": row["contract_address"],
                        "source": SOURCE,
                    }
                else:
                    nfts[key] = {
                        "wallet_id": row["wallet_id"],
                        "contract_address": row["contract_address"],
                        "standard": std,
                        "source": SOURCE,
                    }

        if chunk < chunk_max:
            chunk = min(chunk_max, max(chunk + 1, int(chunk * 1.5)))
        cursor = end + 1

    block_ts_cache: dict[int, int | None] = {}
    for bn in {int(t["block_number"]) for t in transfers}:
        try:
            block_ts_cache[bn] = await rpc.eth_get_block_timestamp(bn)
        except Exception as exc:
            logger.warning("eth_getBlockByNumber failed block=%s: %s", bn, exc)
            block_ts_cache[bn] = None

    for t in transfers:
        ts = block_ts_cache.get(int(t["block_number"]))
        if ts is not None:
            t["block_timestamp"] = datetime.fromtimestamp(
                ts, tz=timezone.utc
            ).isoformat()

    return transfers, list(erc20.values()), list(nfts.values()), to_block


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


def _expand_log(
    log: dict[str, Any],
    addr_to_wallet: dict[str, dict[str, Any]],
    chain_pk: int,
) -> list[tuple[dict[str, Any], str]]:
    topics = [str(t).lower() for t in (log.get("topics") or [])]
    std = classify_transfer(topics)
    if std is None:
        return []

    frm = topic_to_address(topics[1] if len(topics) > 1 else None)
    to = topic_to_address(topics[2] if len(topics) > 2 else None)
    contract = str(log.get("address") or "").lower()
    if not contract.startswith("0x") or len(contract) != 42:
        return []

    matched: dict[int, dict[str, Any]] = {}
    for addr in (frm, to):
        if addr and addr in addr_to_wallet:
            w = addr_to_wallet[addr]
            matched[int(w["wallet_id"])] = w
    if not matched:
        return []

    data = str(log.get("data") or "0x")
    token_id = None
    amount_raw: str | None
    if std == "erc20":
        try:
            amount_raw = str(hex_to_int(data)) if data not in ("0x", "") else "0"
        except Exception:
            amount_raw = "0"
    else:
        try:
            token_id = str(hex_to_int(topics[3]))
        except Exception:
            token_id = None
        amount_raw = "1"

    block_number = hex_to_int(log.get("blockNumber") or "0x0")
    log_index = hex_to_int(log.get("logIndex") or "0x0")
    tx_hash = str(log.get("transactionHash") or "").lower()
    block_hash = str(log.get("blockHash") or "").lower() or None

    rows: list[tuple[dict[str, Any], str]] = []
    for w in matched.values():
        addr = str(w["address"]).lower()
        rows.append(
            (
                {
                    "wallet_id": int(w["wallet_id"]),
                    "chain_id": chain_pk,
                    "contract_address": contract,
                    "standard": std,
                    "transaction_hash": tx_hash,
                    "log_index": log_index,
                    "batch_index": 0,
                    "block_number": block_number,
                    "block_hash": block_hash,
                    "transfer_from": frm,
                    "transfer_to": to,
                    "operator": None,
                    "direction": _direction(addr, frm, to),
                    "token_id": token_id,
                    "amount_raw": amount_raw,
                    "source": SOURCE,
                },
                std,
            )
        )
    return rows
