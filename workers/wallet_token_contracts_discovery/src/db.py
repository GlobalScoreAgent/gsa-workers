"""Supabase Postgres access for wallet_token_contracts_discovery job."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("wallet_token_contracts_discovery")

CLAIM_ROWS_SQL = """
WITH candidates AS (
  SELECT wt.id
  FROM erc_8004.wallet_transactions wt
  JOIN erc_8004.chains c ON c.id = wt.chain_id
  WHERE wt.does_need_discovery_contracts IS DISTINCT FROM FALSE
    AND c.subdomain_alchemy IS NOT NULL
    AND btrim(c.subdomain_alchemy) <> ''
    AND (
      wt.discovery_contracts_claimed_at IS NULL
      OR wt.discovery_contracts_claimed_at
           < NOW() - make_interval(secs => %(stale_seconds)s)
    )
  ORDER BY wt.discovery_contracts_claimed_at NULLS FIRST, wt.id
  LIMIT %(limit)s
  FOR UPDATE OF wt SKIP LOCKED
),
updated AS (
  UPDATE erc_8004.wallet_transactions wt
  SET
    discovery_contracts_claimed_at = NOW(),
    discovery_contracts_claimed_by = %(worker_id)s
  FROM candidates c
  WHERE wt.id = c.id
  RETURNING wt.id, wt.wallet_id, wt.chain_id
)
SELECT
  u.id,
  u.wallet_id,
  u.chain_id,
  w.address,
  ch.subdomain_alchemy
FROM updated u
JOIN erc_8004.wallets w ON w.id = u.wallet_id
JOIN erc_8004.chains ch ON ch.id = u.chain_id
"""

MARK_DONE_SQL = """
UPDATE erc_8004.wallet_transactions
SET
  does_need_discovery_contracts = FALSE,
  discovery_contracts_claimed_at = NULL,
  discovery_contracts_claimed_by = NULL
WHERE id = %(row_id)s
"""

CLEAR_CLAIM_SQL = """
UPDATE erc_8004.wallet_transactions
SET
  discovery_contracts_claimed_at = NULL,
  discovery_contracts_claimed_by = NULL
WHERE id = %(row_id)s
"""

REPLACE_CONTRACTS_SQL = """
SELECT wallets.wallet_token_contracts_replace(
  %(wallet_id)s,
  %(chain_id)s,
  %(rows)s::jsonb
)
"""

CLAIM_MAX_ATTEMPTS = 3
CLAIM_RETRY_BASE_SECONDS = 2.0
RETRYABLE_DB_EXCEPTIONS = (psycopg.OperationalError, psycopg.InterfaceError)
_NO_RECONNECT_EXCEPTIONS = (
    psycopg.errors.QueryCanceled,
    psycopg.errors.DeadlockDetected,
)

T = TypeVar("T")


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

    def _run_with_db_retry(self, operation: str, fn: Callable[[], T]) -> T:
        last_exc: Exception | None = None
        for attempt in range(1, CLAIM_MAX_ATTEMPTS + 1):
            try:
                self.ensure_connected()
                return fn()
            except RETRYABLE_DB_EXCEPTIONS as exc:
                last_exc = exc
                self._safe_rollback()
                if attempt >= CLAIM_MAX_ATTEMPTS:
                    break
                delay = CLAIM_RETRY_BASE_SECONDS * attempt
                if isinstance(exc, _NO_RECONNECT_EXCEPTIONS):
                    logger.warning(
                        "%s attempt %s/%s retryable DB error (%s); retrying in %.1fs",
                        operation,
                        attempt,
                        CLAIM_MAX_ATTEMPTS,
                        exc.__class__.__name__,
                        delay,
                    )
                    time.sleep(delay)
                else:
                    logger.warning(
                        "%s attempt %s/%s connection error (%s); reconnecting in %.1fs",
                        operation,
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

    def claim_rows(
        self,
        worker_id: str,
        limit: int,
        stale_seconds: int,
    ) -> list[dict[str, Any]]:
        def _claim() -> list[dict[str, Any]]:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(
                    CLAIM_ROWS_SQL,
                    {
                        "worker_id": worker_id,
                        "limit": limit,
                        "stale_seconds": stale_seconds,
                    },
                )
                rows = list(cur.fetchall())
            self._conn.commit()
            return rows

        return self._run_with_db_retry("claim", _claim)

    def replace_contracts_and_mark_done(
        self,
        row_id: int,
        wallet_id: int,
        chain_id: int,
        contracts: list[dict[str, str]],
    ) -> str:
        def _save() -> str:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(
                    REPLACE_CONTRACTS_SQL,
                    {
                        "wallet_id": wallet_id,
                        "chain_id": chain_id,
                        "rows": json.dumps(contracts),
                    },
                )
                result = cur.fetchone()
                cur.execute(MARK_DONE_SQL, {"row_id": row_id})
            self._conn.commit()
            if result is None:
                return ""
            # dict_row: first column value
            return str(next(iter(result.values())))

        return self._run_with_db_retry("replace_and_mark_done", _save)

    def clear_claim(self, row_id: int) -> None:
        """Release claim lock without marking done (leave flag pending for retry)."""

        def _clear() -> None:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(CLEAR_CLAIM_SQL, {"row_id": row_id})
            self._conn.commit()

        self._run_with_db_retry("clear_claim", _clear)
