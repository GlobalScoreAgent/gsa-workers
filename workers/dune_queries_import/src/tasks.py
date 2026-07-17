"""Task catalog for Dune → wallets upsert jobs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DuneTask:
    name: str
    query_id: int
    rpc_sql: str


TASKS: tuple[DuneTask, ...] = (
    DuneTask(
        name="cex",
        query_id=7520736,
        rpc_sql="SELECT wallets.cex_addresses_upsert(%(rows)s::jsonb) AS message",
    ),
    DuneTask(
        name="mixers",
        query_id=8015078,
        rpc_sql="SELECT wallets.mixer_addresses_upsert(%(rows)s::jsonb) AS message",
    ),
    DuneTask(
        name="bridges",
        query_id=8015106,
        rpc_sql="SELECT wallets.bridge_addresses_upsert(%(rows)s::jsonb) AS message",
    ),
    DuneTask(
        name="ofac_sanction",
        query_id=8015112,
        rpc_sql=(
            "SELECT wallets.ofac_sanction_addresses_upsert(%(rows)s::jsonb) AS message"
        ),
    ),
)
