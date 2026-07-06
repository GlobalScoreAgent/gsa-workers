"""Supabase Postgres access for wallet_nonce_balance_daily job."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

PENDING_WALLETS_SQL = """
SELECT id, address
FROM erc_8004.wallets
WHERE is_valid_import_current_nonce_and_balance_daily IS TRUE
  AND (
    import_nonce_and_balance_daily_at IS NULL
    OR (import_nonce_and_balance_daily_at AT TIME ZONE 'UTC')::date
       < (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date
  )
  AND id > %(after_id)s
ORDER BY id
LIMIT %(limit)s
"""

CHAINS_ALCHEMY_SQL = """
SELECT chain_id, subdomain_alchemy
FROM erc_8004.chains
WHERE is_active = TRUE
"""

UPDATE_WALLET_SQL = """
UPDATE erc_8004.wallets
SET
  import_current_nonce_and_balance_daily_json = %(payload)s::jsonb,
  import_nonce_and_balance_daily_last_status = %(status)s,
  import_nonce_and_balance_daily_at = NOW(),
  updated_at = NOW()
WHERE id = %(wallet_id)s
"""


class Database:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn: psycopg.Connection | None = None

    def connect(self) -> None:
        self._conn = psycopg.connect(self._dsn, row_factory=dict_row)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def load_alchemy_subdomains(self) -> dict[int, str | None]:
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.execute(CHAINS_ALCHEMY_SQL)
            rows = cur.fetchall()
        mapping: dict[int, str | None] = {}
        for row in rows:
            mapping[int(row["chain_id"])] = row["subdomain_alchemy"]
        return mapping

    def fetch_pending_wallets(self, after_id: int, limit: int) -> list[dict[str, Any]]:
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.execute(PENDING_WALLETS_SQL, {"after_id": after_id, "limit": limit})
            return list(cur.fetchall())

    def save_wallet_result(self, wallet_id: int, payload: str, status: str) -> None:
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.execute(
                UPDATE_WALLET_SQL,
                {"wallet_id": wallet_id, "payload": payload, "status": status},
            )
        self._conn.commit()
