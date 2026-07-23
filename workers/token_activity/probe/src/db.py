"""Supabase Postgres access for token activity probe (15d census)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

import psycopg
from psycopg.rows import dict_row

from networks import EVM_CHAIN_ID_TO_SLUG

logger = logging.getLogger("wallet_token_activity_scan")

CLAIM_ROWS_SQL = """
WITH candidates AS (
  SELECT wt.id
  FROM erc_8004.wallet_transactions wt
  WHERE wt.chain_id = %(chain_pk)s
    AND mod(wt.wallet_id, %(shards)s) = %(shard)s
    AND wt.token_activity_next_eligible_at IS NOT NULL
    AND wt.token_activity_next_eligible_at <= NOW()
    AND wt.does_need_token_activity_enrich IS NOT TRUE
    AND (
      wt.token_activity_claimed_at IS NULL
      OR wt.token_activity_claimed_at
           < NOW() - make_interval(secs => %(stale_seconds)s)
    )
    AND EXISTS (
      SELECT 1
      FROM erc_8004.agent_wallet_tx awt
      JOIN erc_8004.agents a ON a.id = awt.agent_id
      WHERE awt.wallet_id = wt.wallet_id
        AND awt.is_valid IS TRUE
        AND awt.deleted_at IS NULL
        AND a.validation_realness_status = 'valid'
    )
  ORDER BY wt.token_activity_next_eligible_at, wt.id
  LIMIT %(limit)s
  FOR UPDATE OF wt SKIP LOCKED
),
updated AS (
  UPDATE erc_8004.wallet_transactions wt
  SET
    token_activity_claimed_at = NOW(),
    token_activity_claimed_by = %(worker_id)s,
    token_activity_next_eligible_at =
      NOW() + make_interval(secs => %(stale_seconds)s)
  FROM candidates c
  WHERE wt.id = c.id
  RETURNING
    wt.id,
    wt.wallet_id,
    wt.chain_id,
    wt.token_activity_last_scanned_block
)
SELECT
  u.id,
  u.wallet_id,
  u.chain_id,
  u.token_activity_last_scanned_block,
  lower(w.address) AS address
FROM updated u
JOIN erc_8004.wallets w ON w.id = u.wallet_id
"""

MARK_PROBE_DONE_SQL = """
UPDATE erc_8004.wallet_transactions
SET
  token_activity_last_scanned_block = %(last_block)s,
  token_activity_scanned_at = NOW(),
  token_activity_claimed_at = NOW(),
  has_token_activity_error = FALSE,
  token_activity_message_error = NULL,
  token_activity_next_eligible_at = NOW() + interval '15 days',
  does_need_token_activity_enrich = CASE
    WHEN %(enqueue_enrich)s THEN TRUE
    ELSE does_need_token_activity_enrich
  END,
  token_activity_enrich_queued_at = CASE
    WHEN %(enqueue_enrich)s THEN NOW()
    ELSE token_activity_enrich_queued_at
  END
WHERE id = ANY(%(row_ids)s)
"""

MARK_ERROR_SQL = """
UPDATE erc_8004.wallet_transactions
SET
  token_activity_scanned_at = NOW(),
  token_activity_claimed_at = NOW(),
  has_token_activity_error = TRUE,
  token_activity_message_error = %(error_message)s,
  token_activity_next_eligible_at = NOW() + interval '1 hour'
WHERE id = ANY(%(row_ids)s)
"""

ENQUEUE_ENRICH_BY_WALLET_SQL = """
UPDATE erc_8004.wallet_transactions
SET
  does_need_token_activity_enrich = TRUE,
  token_activity_enrich_queued_at = NOW()
WHERE chain_id = %(chain_pk)s
  AND wallet_id = ANY(%(wallet_ids)s)
  AND does_need_token_activity_enrich IS NOT TRUE
RETURNING id
"""

ENQUEUE_ENRICH_NATIVE_DELTAS_SQL = """
WITH latest AS (
  SELECT MAX(snapshot_date) AS d1
  FROM erc_8004.wallet_daily_metrics
  WHERE chain_id = %(chain_pk)s
),
deltas AS (
  SELECT m1.wallet_id
  FROM erc_8004.wallet_daily_metrics m1
  JOIN latest l ON m1.snapshot_date = l.d1
  JOIN erc_8004.wallet_daily_metrics m0
    ON m0.wallet_id = m1.wallet_id
   AND m0.chain_id = m1.chain_id
   AND m0.snapshot_date = l.d1 - 1
  WHERE m1.chain_id = %(chain_pk)s
    AND (
      m1.nonce IS DISTINCT FROM m0.nonce
      OR m1.balance IS DISTINCT FROM m0.balance
    )
)
UPDATE erc_8004.wallet_transactions wt
SET
  does_need_token_activity_enrich = TRUE,
  token_activity_enrich_queued_at = NOW()
FROM deltas d
WHERE wt.wallet_id = d.wallet_id
  AND wt.chain_id = %(chain_pk)s
  AND wt.does_need_token_activity_enrich IS NOT TRUE
  AND EXISTS (
    SELECT 1
    FROM erc_8004.agent_wallet_tx awt
    JOIN erc_8004.agents a ON a.id = awt.agent_id
    WHERE awt.wallet_id = wt.wallet_id
      AND awt.is_valid IS TRUE
      AND awt.deleted_at IS NULL
      AND a.validation_realness_status = 'valid'
  )
RETURNING wt.id
"""

RESOLVE_CHAIN_SQL = """
SELECT id, chain_id, token_activity_runner_count, is_active
FROM erc_8004.chains
WHERE chain_id = %(evm_chain_id)s
LIMIT 1
"""

MATRIX_CHAINS_SQL = """
SELECT chain_id, token_activity_runner_count, is_active
FROM erc_8004.chains
WHERE is_active IS TRUE
  AND chain_id = ANY(%(evm_ids)s)
ORDER BY chain_id
"""

CLAIM_MAX_ATTEMPTS = 3
CLAIM_RETRY_BASE_SECONDS = 2.0
ERROR_MESSAGE_MAX_LEN = 2000
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

    def resolve_chain(self, evm_chain_id: int) -> dict[str, Any]:
        def _resolve() -> dict[str, Any]:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(RESOLVE_CHAIN_SQL, {"evm_chain_id": evm_chain_id})
                row = cur.fetchone()
            self._conn.commit()
            if row is None:
                raise RuntimeError(f"No erc_8004.chains row for evm_chain_id={evm_chain_id}")
            return dict(row)

        return self._run_with_db_retry("resolve_chain", _resolve)

    def list_matrix_cells(self, evm_ids: list[int]) -> list[dict[str, Any]]:
        def _list() -> list[dict[str, Any]]:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(MATRIX_CHAINS_SQL, {"evm_ids": evm_ids})
                rows = list(cur.fetchall())
            self._conn.commit()
            cells: list[dict[str, Any]] = []
            for row in rows:
                slug = EVM_CHAIN_ID_TO_SLUG.get(int(row["chain_id"]))
                if not slug:
                    continue
                # 0 = capacity pause (omit from GHA matrix). NULL/invalid → 1.
                raw = row["token_activity_runner_count"]
                if raw is None:
                    shards = 1
                else:
                    shards = int(raw)
                if shards < 1:
                    continue
                for shard in range(shards):
                    cells.append(
                        {"chain": slug, "shard": shard, "shards": shards}
                    )
            return cells

        return self._run_with_db_retry("list_matrix", _list)

    def claim_rows(
        self,
        *,
        worker_id: str,
        chain_pk: int,
        shard: int,
        shards: int,
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
                        "chain_pk": chain_pk,
                        "shard": shard,
                        "shards": shards,
                        "limit": limit,
                        "stale_seconds": stale_seconds,
                    },
                )
                rows = list(cur.fetchall())
            self._conn.commit()
            return rows

        return self._run_with_db_retry("claim", _claim)

    def mark_probe_done(
        self,
        *,
        row_ids: list[int],
        last_block: int,
        enqueue_enrich_row_ids: list[int],
    ) -> None:
        """Advance probe cursor (+15d). Optionally flag enrich on a subset of row ids."""
        enrich_set = set(enqueue_enrich_row_ids)

        def _mark() -> None:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                quiet_ids = [i for i in row_ids if i not in enrich_set]
                active_ids = [i for i in row_ids if i in enrich_set]
                if quiet_ids:
                    cur.execute(
                        MARK_PROBE_DONE_SQL,
                        {
                            "row_ids": quiet_ids,
                            "last_block": last_block,
                            "enqueue_enrich": False,
                        },
                    )
                if active_ids:
                    cur.execute(
                        MARK_PROBE_DONE_SQL,
                        {
                            "row_ids": active_ids,
                            "last_block": last_block,
                            "enqueue_enrich": True,
                        },
                    )
            self._conn.commit()

        self._run_with_db_retry("mark_probe_done", _mark)

    def enqueue_enrich_for_wallets(
        self, *, chain_pk: int, wallet_ids: list[int]
    ) -> int:
        if not wallet_ids:
            return 0

        def _enq() -> int:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(
                    ENQUEUE_ENRICH_BY_WALLET_SQL,
                    {"chain_pk": chain_pk, "wallet_ids": wallet_ids},
                )
                n = cur.rowcount
            self._conn.commit()
            return int(n or 0)

        return self._run_with_db_retry("enqueue_enrich_wallets", _enq)

    def enqueue_enrich_native_deltas(self, *, chain_pk: int) -> int:
        """Mark enrich from wallet_daily_metrics D vs D-1 on this chain."""

        def _enq() -> int:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(
                    ENQUEUE_ENRICH_NATIVE_DELTAS_SQL,
                    {"chain_pk": chain_pk},
                )
                n = cur.rowcount
            self._conn.commit()
            return int(n or 0)

        return self._run_with_db_retry("enqueue_enrich_native", _enq)

    def mark_error(self, row_ids: list[int], error_message: str) -> None:
        msg = (error_message or "unknown error").strip()
        if len(msg) > ERROR_MESSAGE_MAX_LEN:
            msg = msg[: ERROR_MESSAGE_MAX_LEN - 3] + "..."

        def _mark() -> None:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(
                    MARK_ERROR_SQL,
                    {"row_ids": row_ids, "error_message": msg},
                )
            self._conn.commit()

        self._run_with_db_retry("mark_error", _mark)
