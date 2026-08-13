"""Fill Gnosis (and any missing) timestamps via eth_getBlockByNumber + block_cache."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from alchemy import AlchemyClient
from canonical import parse_int


async def fill_missing_timestamps(
    rows: list[dict[str, Any]],
    *,
    alchemy: AlchemyClient,
    subdomain: str,
    db,
    chain_pk: int,
) -> None:
    missing_blocks: set[int] = set()
    for row in rows:
        if row.get("block_timestamp"):
            continue
        n = parse_int(row.get("block_number"))
        if n is not None:
            missing_blocks.add(n)
    if not missing_blocks:
        return

    cached = db.lookup_block_times(chain_pk, list(missing_blocks))
    still = [b for b in missing_blocks if b not in cached]
    fetched: dict[int, datetime] = {}
    for block_number in still:
        ts = await alchemy.get_block_timestamp(subdomain, block_number)
        if ts is not None:
            fetched[block_number] = ts
    if fetched:
        db.upsert_block_times(chain_pk, fetched)
        cached.update(fetched)

    for row in rows:
        if row.get("block_timestamp"):
            continue
        n = parse_int(row.get("block_number"))
        ts = cached.get(n) if n is not None else None
        if ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            row["block_timestamp"] = ts.astimezone(timezone.utc).isoformat()
