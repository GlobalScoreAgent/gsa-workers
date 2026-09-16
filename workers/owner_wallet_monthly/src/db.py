"""Supabase Postgres access for the owner_wallet_monthly job (monthly + origin lanes).

One connection shared by both lanes under an asyncio lock in job.py. Each lane keeps
its own clock, payload column, status column and snapshot RPC; the only thing they
share here is the retry/reconnect machinery.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("owner_wallet_monthly")

Lane = Literal["monthly", "origin"]

CHAINS_ALCHEMY_SQL = """
SELECT chain_id, subdomain_alchemy
FROM erc_8004.chains
WHERE is_active = TRUE
"""

MONTHLY_ELIGIBLE_WHERE = """
w.is_valid_import_current_nonce_and_balance_monthly IS TRUE
  AND w.import_nonce_and_balance_monthly_next_eligible_at <= NOW()
"""

ORIGIN_ELIGIBLE_WHERE = """
w.is_valid_import_current_nonce_and_balance_monthly IS TRUE
  AND w.import_wallet_history_next_eligible_at <= NOW()
"""

MONTHLY_COUNT_ELIGIBLE_SQL = f"""
SELECT COUNT(*) AS count
FROM erc_8004.wallets w
WHERE {MONTHLY_ELIGIBLE_WHERE}
"""

ORIGIN_COUNT_ELIGIBLE_SQL = f"""
SELECT COUNT(*) AS count
FROM erc_8004.wallets w
WHERE {ORIGIN_ELIGIBLE_WHERE}
"""

MONTHLY_CLAIM_SQL = f"""
WITH candidates AS (
  SELECT w.id
  FROM erc_8004.wallets w
  WHERE {MONTHLY_ELIGIBLE_WHERE}
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

ORIGIN_CLAIM_SQL = f"""
WITH candidates AS (
  SELECT w.id
  FROM erc_8004.wallets w
  WHERE {ORIGIN_ELIGIBLE_WHERE}
  ORDER BY w.import_wallet_history_next_eligible_at, w.id
  LIMIT %(limit)s
  FOR UPDATE SKIP LOCKED
)
UPDATE erc_8004.wallets w
SET
  import_wallet_history_status = 'Pending',
  updated_at = NOW(),
  import_wallet_history_next_eligible_at =
    NOW() + make_interval(secs => %(stale_seconds)s)
FROM candidates c
WHERE w.id = c.id
RETURNING w.id, w.address
"""

MONTHLY_SAVE_SQL = """
UPDATE erc_8004.wallets
SET
  import_current_nonce_and_balance_monthly_json = %(payload)s::jsonb,
  import_nonce_and_balance_monthly_last_status = %(status)s,
  import_nonce_and_balance_monthly_at = NOW(),
  import_nonce_and_balance_monthly_next_eligible_at =
    NOW() + make_interval(secs => %(next_seconds)s),
  updated_at = NOW()
WHERE id = %(wallet_id)s
"""

ORIGIN_SAVE_SQL = """
UPDATE erc_8004.wallets
SET
  import_wallet_history_data = %(payload)s::jsonb,
  import_wallet_history_status = %(status)s,
  import_wallet_history_at = NOW(),
  import_wallet_history_next_eligible_at =
    NOW() + make_interval(secs => %(next_seconds)s),
  updated_at = NOW()
WHERE id = %(wallet_id)s
"""

MONTHLY_REQUEUE_TRANSIENT_SQL = """
UPDATE erc_8004.wallets
SET
  import_nonce_and_balance_monthly_next_eligible_at =
    NOW() + make_interval(secs => %(next_seconds)s),
  updated_at = NOW()
WHERE id = %(wallet_id)s
"""

ORIGIN_REQUEUE_TRANSIENT_SQL = """
UPDATE erc_8004.wallets
SET
  import_wallet_history_next_eligible_at =
    NOW() + make_interval(secs => %(next_seconds)s),
  updated_at = NOW()
WHERE id = %(wallet_id)s
"""

MONTHLY_APPLY_SNAPSHOT_SQL = """
SELECT erc_8004.wallet_apply_monthly_snapshot(%(wallet_id)s)
"""

ORIGIN_APPLY_SNAPSHOT_SQL = """
SELECT erc_8004.wallet_apply_owner_history_snapshot(%(wallet_id)s)
"""

MONTHLY_MARK_SNAPSHOT_ERROR_SQL = """
UPDATE erc_8004.wallets
SET
  import_nonce_and_balance_monthly_last_status = 'Error',
  updated_at = NOW()
WHERE id = %(wallet_id)s
"""

ORIGIN_MARK_SNAPSHOT_ERROR_SQL = """
UPDATE erc_8004.wallets
SET
  import_wallet_history_status = 'Error',
  updated_at = NOW()
WHERE id = %(wallet_id)s
"""

DEFAULT_NEXT_SECONDS = 30 * 24 * 3600

CLAIM_MAX_ATTEMPTS = 3
CLAIM_RETRY_BASE_SECONDS = 2.0
RETRYABLE_DB_EXCEPTIONS = (psycopg.OperationalError, psycopg.InterfaceError)
_NO_RECONNECT_EXCEPTIONS = (
    psycopg.errors.QueryCanceled,
    psycopg.errors.DeadlockDetected,
)

T = TypeVar("T")


@dataclass(frozen=True)
class LaneSql:
    name: str
    count_eligible: str
    claim: str
    save: str
    requeue_transient: str
    apply_snapshot: str
    mark_snapshot_error: str


LANES: dict[str, LaneSql] = {
    "monthly": LaneSql(
        name="monthly",
        count_eligible=MONTHLY_COUNT_ELIGIBLE_SQL,
        claim=MONTHLY_CLAIM_SQL,
        save=MONTHLY_SAVE_SQL,
        requeue_transient=MONTHLY_REQUEUE_TRANSIENT_SQL,
        apply_snapshot=MONTHLY_APPLY_SNAPSHOT_SQL,
        mark_snapshot_error=MONTHLY_MARK_SNAPSHOT_ERROR_SQL,
    ),
    "origin": LaneSql(
        name="origin",
        count_eligible=ORIGIN_COUNT_ELIGIBLE_SQL,
        claim=ORIGIN_CLAIM_SQL,
        save=ORIGIN_SAVE_SQL,
        requeue_transient=ORIGIN_REQUEUE_TRANSIENT_SQL,
        apply_snapshot=ORIGIN_APPLY_SNAPSHOT_SQL,
        mark_snapshot_error=ORIGIN_MARK_SNAPSHOT_ERROR_SQL,
    ),
}


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

    def load_alchemy_subdomains(self) -> dict[int, str | None]:
        def _load() -> dict[int, str | None]:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(CHAINS_ALCHEMY_SQL)
                rows = cur.fetchall()
            return {int(row["chain_id"]): row["subdomain_alchemy"] for row in rows}

        return self._run_with_db_retry("load_alchemy_subdomains", _load)

    def count_eligible_wallets(self, lane: Lane) -> int:
        sql = LANES[lane]

        def _count() -> int:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(sql.count_eligible)
                row = cur.fetchone()
            return int(row["count"]) if row else 0

        return self._run_with_db_retry(f"count_eligible[{lane}]", _count)

    def claim_wallets(
        self,
        lane: Lane,
        limit: int,
        stale_seconds: int,
    ) -> list[dict[str, Any]]:
        sql = LANES[lane]

        def _claim() -> list[dict[str, Any]]:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(sql.claim, {"limit": limit, "stale_seconds": stale_seconds})
                rows = list(cur.fetchall())
            self._conn.commit()
            return rows

        return self._run_with_db_retry(f"claim[{lane}]", _claim)

    def save_results_batch(
        self,
        lane: Lane,
        results: list[tuple[int, str, str, int]],
    ) -> None:
        """Persist payload + status. Each row carries its own next-eligibility window."""
        if not results:
            return

        sql = LANES[lane]
        params = [
            {
                "wallet_id": wallet_id,
                "payload": payload,
                "status": status,
                "next_seconds": next_seconds,
            }
            for wallet_id, payload, status, next_seconds in results
        ]

        def _save() -> None:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.executemany(sql.save, params)
            self._conn.commit()

        self._run_with_db_retry(f"save_batch[{lane}]", _save)

    def requeue_transient(
        self,
        lane: Lane,
        wallet_ids: list[int],
        next_seconds: int,
    ) -> None:
        """Short-requeue wallets whose chains all failed on rate limit: no payload, no Error."""
        if not wallet_ids:
            return

        sql = LANES[lane]
        params = [
            {"wallet_id": wallet_id, "next_seconds": next_seconds}
            for wallet_id in wallet_ids
        ]

        def _requeue() -> None:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.executemany(sql.requeue_transient, params)
            self._conn.commit()

        self._run_with_db_retry(f"requeue_transient[{lane}]", _requeue)

    def apply_snapshots(self, lane: Lane, wallet_ids: list[int]) -> list[int]:
        """Run the lane snapshot RPC; return wallet ids that failed."""
        if not wallet_ids:
            return []

        sql = LANES[lane]
        failed: list[int] = []

        for wallet_id in wallet_ids:
            applied = False
            for attempt in range(1, CLAIM_MAX_ATTEMPTS + 1):
                try:
                    self.ensure_connected()
                    assert self._conn is not None
                    with self._conn.cursor() as cur:
                        cur.execute(sql.apply_snapshot, {"wallet_id": wallet_id})
                    applied = True
                    break
                except RETRYABLE_DB_EXCEPTIONS as exc:
                    self._safe_rollback()
                    if attempt >= CLAIM_MAX_ATTEMPTS:
                        logger.warning(
                            "Snapshot[%s] failed for wallet id=%s after %s retries: %s",
                            lane,
                            wallet_id,
                            CLAIM_MAX_ATTEMPTS,
                            exc,
                        )
                        failed.append(wallet_id)
                        break
                    delay = CLAIM_RETRY_BASE_SECONDS * attempt
                    if isinstance(exc, _NO_RECONNECT_EXCEPTIONS):
                        logger.warning(
                            "Snapshot[%s] wallet id=%s attempt %s/%s %s; retrying in %.1fs",
                            lane,
                            wallet_id,
                            attempt,
                            CLAIM_MAX_ATTEMPTS,
                            exc.__class__.__name__,
                            delay,
                        )
                        time.sleep(delay)
                    else:
                        logger.warning(
                            "Snapshot[%s] wallet id=%s attempt %s/%s connection error; "
                            "reconnecting in %.1fs",
                            lane,
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
                        "Snapshot[%s] failed for wallet id=%s: %s",
                        lane,
                        wallet_id,
                        exc,
                    )
                    failed.append(wallet_id)
                    break

            if not applied and wallet_id not in failed:
                failed.append(wallet_id)

        if failed:
            self._mark_snapshot_errors(lane, failed)

        def _commit() -> None:
            assert self._conn is not None
            self._conn.commit()

        self._run_with_db_retry(f"snapshot_commit[{lane}]", _commit)
        return failed

    def _mark_snapshot_errors(self, lane: Lane, wallet_ids: list[int]) -> None:
        sql = LANES[lane]
        params = [{"wallet_id": wallet_id} for wallet_id in wallet_ids]

        def _mark() -> None:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.executemany(sql.mark_snapshot_error, params)

        self._run_with_db_retry(f"mark_snapshot_errors[{lane}]", _mark)
