"""Supabase Postgres access for wallet_token_activity_scan."""

from __future__ import annotations

import json
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

MARK_DONE_SQL = """
UPDATE erc_8004.wallet_transactions
SET
  token_activity_last_scanned_block = %(last_block)s,
  token_activity_scanned_at = NOW(),
  token_activity_claimed_at = NOW(),
  has_token_activity_error = FALSE,
  token_activity_message_error = NULL,
  token_activity_next_eligible_at = NOW() + interval '1 day'
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

UPSERT_ERC20_SQL = """
SELECT wallets.wallet_token_contracts_upsert(
  %(wallet_id)s,
  %(chain_id)s,
  %(rows)s::jsonb
)
"""

UPSERT_NFT_SQL = """
SELECT wallets.wallet_nft_contracts_upsert(
  %(wallet_id)s,
  %(chain_id)s,
  %(rows)s::jsonb
)
"""

UPSERT_TRANSFERS_SQL = """
SELECT wallets.wallet_token_transfers_upsert(%(rows)s::jsonb)
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
                shards = max(1, int(row["token_activity_runner_count"] or 1))
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

    def persist_batch_and_mark_done(
        self,
        *,
        row_ids: list[int],
        chain_pk: int,
        last_block: int,
        transfers: list[dict[str, Any]],
        erc20_by_wallet: dict[int, list[dict[str, str]]],
        nft_by_wallet: dict[int, list[dict[str, str]]],
    ) -> str:
        def _save() -> str:
            assert self._conn is not None
            msgs: list[str] = []
            with self._conn.cursor() as cur:
                if transfers:
                    cur.execute(
                        UPSERT_TRANSFERS_SQL,
                        {"rows": json.dumps(transfers)},
                    )
                    r = cur.fetchone()
                    if r:
                        msgs.append(str(next(iter(r.values()))))

                for wallet_id, rows in erc20_by_wallet.items():
                    if not rows:
                        continue
                    cur.execute(
                        UPSERT_ERC20_SQL,
                        {
                            "wallet_id": wallet_id,
                            "chain_id": chain_pk,
                            "rows": json.dumps(rows),
                        },
                    )
                    r = cur.fetchone()
                    if r:
                        msgs.append(str(next(iter(r.values()))))

                for wallet_id, rows in nft_by_wallet.items():
                    if not rows:
                        continue
                    cur.execute(
                        UPSERT_NFT_SQL,
                        {
                            "wallet_id": wallet_id,
                            "chain_id": chain_pk,
                            "rows": json.dumps(rows),
                        },
                    )
                    r = cur.fetchone()
                    if r:
                        msgs.append(str(next(iter(r.values()))))

                cur.execute(
                    MARK_DONE_SQL,
                    {"row_ids": row_ids, "last_block": last_block},
                )
            self._conn.commit()
            return " | ".join(msgs) if msgs else "ok empty"

        return self._run_with_db_retry("persist_and_mark_done", _save)

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
