"""Supabase Postgres access for owner_wallet_nonce_balance_monthly job."""

from __future__ import annotations

import logging
import time
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("owner_wallet_nonce_balance_monthly")

ELIGIBLE_WHERE = """
w.is_valid_import_current_nonce_and_balance_monthly IS TRUE
  AND w.import_nonce_and_balance_monthly_next_eligible_at <= NOW()
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
  ORDER BY w.import_nonce_and_balance_monthly_next_eligible_at, w.id
  LIMIT %(limit)s
  FOR UPDATE SKIP LOCKED
)
UPDATE erc_8004.wallets w
SET
  import_nonce_and_balance_monthly_last_status = 'Pending',
  updated_at = NOW(),
  import_nonce_and_balance_monthly_next_eligible_at =
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
  import_current_nonce_and_balance_monthly_json = %(payload)s::jsonb,
  import_nonce_and_balance_monthly_last_status = %(status)s,
  import_nonce_and_balance_monthly_at = NOW(),
  import_nonce_and_balance_monthly_next_eligible_at = NOW() + INTERVAL '30 days',
  updated_at = NOW()
WHERE id = %(wallet_id)s
"""

APPLY_MONTHLY_SNAPSHOT_SQL = """
SELECT erc_8004.wallet_apply_monthly_snapshot(%(wallet_id)s)
"""

MARK_SNAPSHOT_ERROR_SQL = """
UPDATE erc_8004.wallets
SET
  import_nonce_and_balance_monthly_last_status = 'Error',
  updated_at = NOW()
WHERE id = %(wallet_id)s
"""

CLAIM_MAX_ATTEMPTS = 3
CLAIM_RETRY_BASE_SECONDS = 2.0
CONN_RETRY_EXCEPTIONS = (psycopg.OperationalError, psycopg.InterfaceError)


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
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _reconnect(self) -> None:
        logger.warning("Reconnecting to Postgres after connection failure")
        self.close()
        self.connect()

    def ensure_connected(self) -> None:
        if self._conn is None or self._conn.closed:
            self._reconnect()

    def _safe_rollback(self) -> None:
        if self._conn is None or self._conn.closed:
            return
        try:
            self._conn.rollback()
        except Exception:
            pass

    def load_alchemy_subdomains(self) -> dict[int, str | None]:
        self.ensure_connected()
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.execute(CHAINS_ALCHEMY_SQL)
            rows = cur.fetchall()
        mapping: dict[int, str | None] = {}
        for row in rows:
            mapping[int(row["chain_id"])] = row["subdomain_alchemy"]
        return mapping

    def count_eligible_wallets(self, stale_seconds: int) -> int:
        self.ensure_connected()
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.execute(COUNT_ELIGIBLE_SQL, {"stale_seconds": stale_seconds})
            row = cur.fetchone()
        return int(row["count"]) if row else 0

    def claim_wallets(self, limit: int, stale_seconds: int) -> list[dict[str, Any]]:
        last_exc: Exception | None = None

        for attempt in range(1, CLAIM_MAX_ATTEMPTS + 1):
            try:
                self.ensure_connected()
                assert self._conn is not None
                with self._conn.cursor() as cur:
                    cur.execute(
                        CLAIM_WALLETS_SQL,
                        {"limit": limit, "stale_seconds": stale_seconds},
                    )
                    rows = list(cur.fetchall())
                self._conn.commit()
                return rows
            except psycopg.errors.QueryCanceled as exc:
                last_exc = exc
                self._safe_rollback()
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
            except CONN_RETRY_EXCEPTIONS as exc:
                last_exc = exc
                self._safe_rollback()
                if attempt >= CLAIM_MAX_ATTEMPTS:
                    break
                delay = CLAIM_RETRY_BASE_SECONDS * attempt
                logger.warning(
                    "Claim attempt %s/%s connection error (%s); reconnecting in %.1fs",
                    attempt,
                    CLAIM_MAX_ATTEMPTS,
                    exc,
                    delay,
                )
                time.sleep(delay)
                self._reconnect()

        assert last_exc is not None
        raise last_exc

    def save_wallet_result(self, wallet_id: int, payload: str, status: str) -> None:
        self.save_wallet_results_batch([(wallet_id, payload, status)])

    def save_wallet_results_batch(
        self,
        results: list[tuple[int, str, str]],
    ) -> None:
        if not results:
            return

        last_exc: Exception | None = None
        params = [
            {"wallet_id": wallet_id, "payload": payload, "status": status}
            for wallet_id, payload, status in results
        ]

        for attempt in range(1, CLAIM_MAX_ATTEMPTS + 1):
            try:
                self.ensure_connected()
                assert self._conn is not None
                with self._conn.cursor() as cur:
                    cur.executemany(UPDATE_WALLET_SQL, params)
                self._conn.commit()
                return
            except CONN_RETRY_EXCEPTIONS as exc:
                last_exc = exc
                self._safe_rollback()
                if attempt >= CLAIM_MAX_ATTEMPTS:
                    break
                delay = CLAIM_RETRY_BASE_SECONDS * attempt
                logger.warning(
                    "Save batch attempt %s/%s connection error (%s); reconnecting in %.1fs",
                    attempt,
                    CLAIM_MAX_ATTEMPTS,
                    exc,
                    delay,
                )
                time.sleep(delay)
                self._reconnect()
            except Exception:
                self._safe_rollback()
                raise

        assert last_exc is not None
        raise last_exc

    def apply_monthly_snapshots(self, wallet_ids: list[int]) -> list[int]:
        """Run wallet_apply_monthly_snapshot; return wallet ids that failed."""
        if not wallet_ids:
            return []

        failed: list[int] = []

        for wallet_id in wallet_ids:
            applied = False
            for attempt in range(1, CLAIM_MAX_ATTEMPTS + 1):
                try:
                    self.ensure_connected()
                    assert self._conn is not None
                    with self._conn.cursor() as cur:
                        cur.execute(
                            APPLY_MONTHLY_SNAPSHOT_SQL,
                            {"wallet_id": wallet_id},
                        )
                    applied = True
                    break
                except CONN_RETRY_EXCEPTIONS as exc:
                    self._safe_rollback()
                    if attempt >= CLAIM_MAX_ATTEMPTS:
                        logger.warning(
                            "Snapshot failed for wallet id=%s after %s connection retries: %s",
                            wallet_id,
                            CLAIM_MAX_ATTEMPTS,
                            exc,
                        )
                        failed.append(wallet_id)
                        break
                    delay = CLAIM_RETRY_BASE_SECONDS * attempt
                    logger.warning(
                        "Snapshot wallet id=%s attempt %s/%s connection error; reconnecting in %.1fs",
                        wallet_id,
                        attempt,
                        CLAIM_MAX_ATTEMPTS,
                        delay,
                    )
                    time.sleep(delay)
                    self._reconnect()
                except Exception as exc:
                    self._safe_rollback()
                    logger.warning(
                        "Snapshot failed for wallet id=%s: %s",
                        wallet_id,
                        exc,
                    )
                    failed.append(wallet_id)
                    break

            if not applied and wallet_id not in failed:
                failed.append(wallet_id)

        if failed:
            self._mark_snapshot_errors(failed)

        self.ensure_connected()
        assert self._conn is not None
        self._conn.commit()
        return failed

    def _mark_snapshot_errors(self, wallet_ids: list[int]) -> None:
        last_exc: Exception | None = None
        params = [{"wallet_id": wallet_id} for wallet_id in wallet_ids]

        for attempt in range(1, CLAIM_MAX_ATTEMPTS + 1):
            try:
                self.ensure_connected()
                assert self._conn is not None
                with self._conn.cursor() as cur:
                    cur.executemany(MARK_SNAPSHOT_ERROR_SQL, params)
                return
            except CONN_RETRY_EXCEPTIONS as exc:
                last_exc = exc
                self._safe_rollback()
                if attempt >= CLAIM_MAX_ATTEMPTS:
                    break
                delay = CLAIM_RETRY_BASE_SECONDS * attempt
                logger.warning(
                    "Mark snapshot errors attempt %s/%s connection error; reconnecting in %.1fs",
                    attempt,
                    CLAIM_MAX_ATTEMPTS,
                    delay,
                )
                time.sleep(delay)
                self._reconnect()

        if last_exc is not None:
            raise last_exc
