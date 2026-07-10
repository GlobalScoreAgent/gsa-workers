"""Supabase Postgres access for wallet_nonce_balance_daily job."""

from __future__ import annotations

import logging
import time
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("wallet_nonce_balance_daily")

ELIGIBLE_WHERE = """
w.is_valid_import_current_nonce_and_balance_daily IS TRUE
  AND w.import_nonce_and_balance_daily_next_eligible_at <= NOW()
"""

CLAIM_WALLETS_SQL = f"""
WITH candidates AS (
  SELECT w.id
  FROM erc_8004.wallets w
  WHERE {ELIGIBLE_WHERE}
  ORDER BY w.import_nonce_and_balance_daily_next_eligible_at, w.id
  LIMIT %(limit)s
  FOR UPDATE SKIP LOCKED
)
UPDATE erc_8004.wallets w
SET
  import_nonce_and_balance_daily_last_status = 'Pending',
  import_nonce_and_balance_daily_claimed_at = NOW(),
  import_nonce_and_balance_daily_claimed_by = %(worker_id)s,
  updated_at = NOW(),
  import_nonce_and_balance_daily_next_eligible_at =
    NOW() + make_interval(secs => %(stale_seconds)s)
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
  import_current_nonce_and_balance_daily_json = %(payload)s::jsonb,
  import_nonce_and_balance_daily_last_status = %(status)s,
  import_nonce_and_balance_daily_at = NOW(),
  import_nonce_and_balance_daily_claimed_at = NULL,
  import_nonce_and_balance_daily_claimed_by = NULL,
  import_nonce_and_balance_daily_next_eligible_at =
    ((NOW() AT TIME ZONE 'UTC')::date + INTERVAL '1 day')::timestamp AT TIME ZONE 'UTC',
  updated_at = NOW()
WHERE id = %(wallet_id)s
"""

APPLY_DAILY_SNAPSHOT_SQL = """
SELECT erc_8004.wallet_apply_daily_snapshot(%(wallet_id)s)
"""

MARK_SNAPSHOT_ERROR_SQL = """
UPDATE erc_8004.wallets
SET
  import_nonce_and_balance_daily_last_status = 'Error',
  updated_at = NOW()
WHERE id = %(wallet_id)s
"""

CLAIM_MAX_ATTEMPTS = 3
CLAIM_RETRY_BASE_SECONDS = 2.0


class Database:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn: psycopg.Connection | None = None

    def connect(self) -> None:
        self._conn = psycopg.connect(self._dsn, row_factory=dict_row)
        with self._conn.cursor() as cur:
            cur.execute("SET statement_timeout = '300s'")

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

    def claim_wallets(
        self,
        worker_id: str,
        limit: int,
        stale_seconds: int,
    ) -> list[dict[str, Any]]:
        assert self._conn is not None
        last_exc: Exception | None = None

        for attempt in range(1, CLAIM_MAX_ATTEMPTS + 1):
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        CLAIM_WALLETS_SQL,
                        {
                            "worker_id": worker_id,
                            "limit": limit,
                            "stale_seconds": stale_seconds,
                        },
                    )
                    rows = list(cur.fetchall())
                self._conn.commit()
                return rows
            except psycopg.errors.QueryCanceled as exc:
                last_exc = exc
                self._conn.rollback()
                if attempt >= CLAIM_MAX_ATTEMPTS:
                    break
                delay = CLAIM_RETRY_BASE_SECONDS * attempt
                logger.warning(
                    "Claim attempt %s/%s timed out; retrying in %.1fs",
                    attempt,
                    CLAIM_MAX_ATTEMPTS,
                    delay,
                )
                time.sleep(delay)

        assert last_exc is not None
        raise last_exc

    def save_wallet_result(self, wallet_id: int, payload: str, status: str) -> None:
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.execute(
                UPDATE_WALLET_SQL,
                {"wallet_id": wallet_id, "payload": payload, "status": status},
            )
        self._conn.commit()

    def save_wallet_results_batch(
        self,
        results: list[tuple[int, str, str]],
    ) -> None:
        if not results:
            return
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.executemany(
                UPDATE_WALLET_SQL,
                [
                    {"wallet_id": wallet_id, "payload": payload, "status": status}
                    for wallet_id, payload, status in results
                ],
            )
        self._conn.commit()

    def apply_daily_snapshots(self, wallet_ids: list[int]) -> list[int]:
        """Run wallet_apply_daily_snapshot; return wallet ids that failed."""
        if not wallet_ids:
            return []

        assert self._conn is not None
        failed: list[int] = []

        for wallet_id in wallet_ids:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(APPLY_DAILY_SNAPSHOT_SQL, {"wallet_id": wallet_id})
            except Exception as exc:
                logger.warning(
                    "Snapshot failed for wallet id=%s: %s",
                    wallet_id,
                    exc,
                )
                failed.append(wallet_id)

        if failed:
            with self._conn.cursor() as cur:
                cur.executemany(
                    MARK_SNAPSHOT_ERROR_SQL,
                    [{"wallet_id": wallet_id} for wallet_id in failed],
                )

        self._conn.commit()
        return failed
