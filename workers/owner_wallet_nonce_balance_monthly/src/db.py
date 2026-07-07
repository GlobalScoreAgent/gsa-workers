"""Supabase Postgres access for owner_wallet_nonce_balance_monthly job."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

ELIGIBLE_WHERE = """
w.is_valid_import_current_nonce_and_balance_monthly IS TRUE
  AND (
    w.import_nonce_and_balance_monthly_at IS NULL
    OR w.import_nonce_and_balance_monthly_at < NOW() - INTERVAL '30 days'
  )
  AND (
    w.import_nonce_and_balance_monthly_last_status IS NULL
    OR w.import_nonce_and_balance_monthly_last_status IN ('Completed', 'Error', 'Processed')
    OR (
      w.import_nonce_and_balance_monthly_last_status = 'Pending'
      AND w.updated_at < NOW() - make_interval(secs => %(stale_seconds)s)
    )
  )
"""

COUNT_ELIGIBLE_SQL = f"""
SELECT COUNT(*) AS count
FROM erc_8004.wallets w
WHERE {ELIGIBLE_WHERE}
"""

CLAIM_WALLETS_SQL = f"""
WITH candidates AS (
  SELECT w.id
  FROM erc_8004.wallets w
  WHERE {ELIGIBLE_WHERE}
  ORDER BY w.id
  LIMIT %(limit)s
  FOR UPDATE SKIP LOCKED
)
UPDATE erc_8004.wallets w
SET
  import_nonce_and_balance_monthly_last_status = 'Pending',
  updated_at = NOW()
FROM candidates c
WHERE w.id = c.id
RETURNING w.id, w.address
"""

CHAINS_ALCHEMY_SQL = """
SELECT chain_id, subdomain_alchemy
FROM erc_8004.chains
WHERE is_active = TRUE
"""

UPDATE_WALLET_SQL = """
UPDATE erc_8004.wallets
SET
  import_current_nonce_and_balance_monthly_json = %(payload)s::jsonb,
  import_nonce_and_balance_monthly_last_status = %(status)s,
  import_nonce_and_balance_monthly_at = NOW(),
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

    def count_eligible_wallets(self, stale_seconds: int) -> int:
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.execute(COUNT_ELIGIBLE_SQL, {"stale_seconds": stale_seconds})
            row = cur.fetchone()
        return int(row["count"]) if row else 0

    def claim_wallets(self, limit: int, stale_seconds: int) -> list[dict[str, Any]]:
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.execute(
                CLAIM_WALLETS_SQL,
                {"limit": limit, "stale_seconds": stale_seconds},
            )
            rows = list(cur.fetchall())
        self._conn.commit()
        return rows

    def save_wallet_result(self, wallet_id: int, payload: str, status: str) -> None:
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.execute(
                UPDATE_WALLET_SQL,
                {"wallet_id": wallet_id, "payload": payload, "status": status},
            )
        self._conn.commit()
