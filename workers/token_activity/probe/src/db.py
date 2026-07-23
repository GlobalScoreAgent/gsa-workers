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

# Two-phase claim: cheap due index prefilter, then nested-loop validity via
# idx_agent_wallet_tx_wallet_active. Use `awt.is_valid` (not `IS TRUE`) so the
# partial index matches — otherwise Postgres seq-scans agent_wallet_tx (~15s+).
CLAIM_ROWS_SQL = """
WITH rough AS MATERIALIZED (
  SELECT wt.id, wt.wallet_id, wt.token_activity_next_eligible_at
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
  ORDER BY wt.token_activity_next_eligible_at, wt.id
  LIMIT GREATEST(%(limit)s * 5, %(limit)s)
),
filtered AS MATERIALIZED (
  SELECT r.id, r.token_activity_next_eligible_at
  FROM rough r
  WHERE EXISTS (
    SELECT 1
    FROM erc_8004.agent_wallet_tx awt
    JOIN erc_8004.agents a
      ON a.id = awt.agent_id
     AND a.validation_realness_status = 'valid'
    WHERE awt.wallet_id = r.wallet_id
      AND awt.is_valid
      AND awt.deleted_at IS NULL
  )
  ORDER BY r.token_activity_next_eligible_at, r.id
  LIMIT %(limit)s
),
candidates AS (
  SELECT wt.id
  FROM erc_8004.wallet_transactions wt
  JOIN filtered f ON f.id = wt.id
  ORDER BY f.token_activity_next_eligible_at, wt.id
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

# BSC helper / any-shard claim: no mod() filter; SKIP LOCKED vs dedicated shards.
CLAIM_ROWS_HELPER_SQL = """
WITH rough AS MATERIALIZED (
  SELECT wt.id, wt.wallet_id, wt.token_activity_next_eligible_at
  FROM erc_8004.wallet_transactions wt
  WHERE wt.chain_id = %(chain_pk)s
    AND wt.token_activity_next_eligible_at IS NOT NULL
    AND wt.token_activity_next_eligible_at <= NOW()
    AND wt.does_need_token_activity_enrich IS NOT TRUE
    AND (
      wt.token_activity_claimed_at IS NULL
      OR wt.token_activity_claimed_at
           < NOW() - make_interval(secs => %(stale_seconds)s)
    )
  ORDER BY wt.token_activity_next_eligible_at, wt.id
  LIMIT GREATEST(%(limit)s * 5, %(limit)s)
),
filtered AS MATERIALIZED (
  SELECT r.id, r.token_activity_next_eligible_at
  FROM rough r
  WHERE EXISTS (
    SELECT 1
    FROM erc_8004.agent_wallet_tx awt
    JOIN erc_8004.agents a
      ON a.id = awt.agent_id
     AND a.validation_realness_status = 'valid'
    WHERE awt.wallet_id = r.wallet_id
      AND awt.is_valid
      AND awt.deleted_at IS NULL
  )
  ORDER BY r.token_activity_next_eligible_at, r.id
  LIMIT %(limit)s
),
candidates AS (
  SELECT wt.id
  FROM erc_8004.wallet_transactions wt
  JOIN filtered f ON f.id = wt.id
  ORDER BY f.token_activity_next_eligible_at, wt.id
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

# Soft release after transient RPC rate limits — do not burn a census error.
RELEASE_CLAIM_SQL = """
UPDATE erc_8004.wallet_transactions
SET
  token_activity_claimed_at = NULL,
  token_activity_claimed_by = NULL,
  token_activity_next_eligible_at =
    NOW() + make_interval(secs => %(delay_seconds)s),
  has_token_activity_error = FALSE,
  token_activity_message_error = NULL
WHERE id = ANY(%(row_ids)s)
"""

# Hygiene at drain start: clear sticky error flags on rows already due again.
# Does not advance next_eligible (no thundering herd of early retries).
CLEAR_DUE_ERRORS_SQL = """
UPDATE erc_8004.wallet_transactions
SET
  has_token_activity_error = FALSE,
  token_activity_message_error = NULL
WHERE chain_id = %(chain_pk)s
  AND has_token_activity_error IS TRUE
  AND token_activity_next_eligible_at IS NOT NULL
  AND token_activity_next_eligible_at <= NOW()
RETURNING id
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
# Namespace for pg_advisory_xact_lock(k1, chain_pk) on BSC claims.
CLAIM_ADVISORY_K1 = 800415  # token_activity claim
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
        helper: bool = False,
        serialize_claim: bool = False,
    ) -> list[dict[str, Any]]:
        sql = CLAIM_ROWS_HELPER_SQL if helper else CLAIM_ROWS_SQL

        def _claim() -> list[dict[str, Any]]:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                if serialize_claim:
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(%s, %s)",
                        (CLAIM_ADVISORY_K1, int(chain_pk)),
                    )
                params: dict[str, Any] = {
                    "worker_id": worker_id,
                    "chain_pk": chain_pk,
                    "limit": limit,
                    "stale_seconds": stale_seconds,
                }
                if not helper:
                    params["shard"] = shard
                    params["shards"] = shards
                cur.execute(sql, params)
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

    def release_claim(self, row_ids: list[int], *, delay_seconds: int = 300) -> None:
        """Return rows to the due queue soon (rate-limit / transient RPC)."""
        if not row_ids:
            return
        delay = max(60, int(delay_seconds))

        def _release() -> None:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(
                    RELEASE_CLAIM_SQL,
                    {"row_ids": row_ids, "delay_seconds": delay},
                )
            self._conn.commit()

        self._run_with_db_retry("release_claim", _release)

    def clear_due_errors(self, *, chain_pk: int) -> int:
        """Clear error flags on this chain for rows already past next_eligible."""

        def _clear() -> int:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(CLEAR_DUE_ERRORS_SQL, {"chain_pk": chain_pk})
                n = cur.rowcount
            self._conn.commit()
            return int(n or 0)

        return self._run_with_db_retry("clear_due_errors", _clear)
