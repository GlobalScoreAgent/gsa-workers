"""Postgres access for agent_series_export."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import date
from typing import Any, TypeVar

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("agent_series_export")

CLAIM_MAX_ATTEMPTS = 3
CLAIM_RETRY_BASE_SECONDS = 2.0
RETRYABLE_DB_EXCEPTIONS = (psycopg.OperationalError, psycopg.InterfaceError)
_NO_RECONNECT_EXCEPTIONS = (
    psycopg.errors.QueryCanceled,
    psycopg.errors.DeadlockDetected,
)

# T-1 resuelto en la base y no en el runner: las dos lanes tienen que coincidir en
# el as_of aunque arranquen con minutos de diferencia o el runner este en otra TZ.
AS_OF_SQL = """
SELECT ((now() AT TIME ZONE 'utc')::date - 1) AS as_of
"""

CYCLE_OPEN_SQL = """
SELECT erc_8004.agent_series_cycle_open(%(as_of)s::date) AS cycle
"""

SCALARS_REFRESH_SQL = """
SELECT agents_scanned, rows_upserted, last_agent_id
FROM erc_8004.agent_tx_scalars_refresh(
  %(as_of)s::date,
  %(batch)s,
  %(after_agent_id)s
)
"""

# document::text: el arbol viaja como texto y se sube tal cual. Parsearlo a dict
# convertiria los balances numeric a float y el JSON subido perderia decimales.
CLAIM_SQL = """
SELECT agent_id, document::text AS document
FROM erc_8004.agent_series_claim(
  %(limit)s,
  %(as_of)s::date,
  %(worker_id)s,
  %(stale_seconds)s
)
"""

ACK_SQL = """
SELECT erc_8004.agent_series_ack(%(rows)s::jsonb, %(as_of)s::date) AS acked
"""

RELEASE_SQL = """
SELECT erc_8004.agent_series_release(%(agent_ids)s::bigint[]) AS released
"""

CYCLE_CLOSE_SQL = """
SELECT erc_8004.agent_series_cycle_close(%(as_of)s::date) AS cycle
"""

T = TypeVar("T")


class Database:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn: psycopg.Connection | None = None

    def connect(self) -> None:
        self._conn = psycopg.connect(self._dsn, row_factory=dict_row)
        with self._conn.cursor() as cur:
            # Por encima del statement_timeout interno del claim (600s) para que el
            # corte lo decida la funcion y no la sesion.
            cur.execute("SET statement_timeout = '900s'")

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
                        exc.__class__.__name__,
                        delay,
                    )
                    time.sleep(delay)
                    self._reconnect()
            except Exception:
                self._safe_rollback()
                raise

        assert last_exc is not None
        raise last_exc

    def resolve_as_of(self) -> date:
        def _resolve() -> date:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(AS_OF_SQL)
                row = cur.fetchone()
            self._conn.commit()
            assert row is not None
            return row["as_of"]

        return self._run_with_db_retry("resolve_as_of", _resolve)

    def cycle_open(self, as_of: date) -> dict[str, Any]:
        def _open() -> dict[str, Any]:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(CYCLE_OPEN_SQL, {"as_of": as_of})
                row = cur.fetchone()
            self._conn.commit()
            return dict(row["cycle"]) if row and row["cycle"] else {}

        return self._run_with_db_retry("cycle_open", _open)

    def refresh_scalars(
        self, as_of: date, batch: int, after_agent_id: int
    ) -> dict[str, Any]:
        def _refresh() -> dict[str, Any]:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(
                    SCALARS_REFRESH_SQL,
                    {"as_of": as_of, "batch": batch, "after_agent_id": after_agent_id},
                )
                row = cur.fetchone()
            self._conn.commit()
            assert row is not None
            return row

        return self._run_with_db_retry("refresh_scalars", _refresh)

    def claim_rows(
        self,
        as_of: date,
        worker_id: str,
        limit: int,
        stale_seconds: int,
    ) -> list[dict[str, Any]]:
        def _claim() -> list[dict[str, Any]]:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(
                    CLAIM_SQL,
                    {
                        "limit": limit,
                        "as_of": as_of,
                        "worker_id": worker_id,
                        "stale_seconds": stale_seconds,
                    },
                )
                rows = list(cur.fetchall())
            self._conn.commit()
            return rows

        return self._run_with_db_retry("claim", _claim)

    def ack(self, rows: list[dict[str, Any]], as_of: date) -> int:
        if not rows:
            return 0

        def _ack() -> int:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(ACK_SQL, {"rows": json.dumps(rows), "as_of": as_of})
                row = cur.fetchone()
            self._conn.commit()
            return int(row["acked"]) if row else 0

        return self._run_with_db_retry("ack", _ack)

    def release(self, agent_ids: list[int]) -> int:
        if not agent_ids:
            return 0

        def _release() -> int:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(RELEASE_SQL, {"agent_ids": agent_ids})
                row = cur.fetchone()
            self._conn.commit()
            return int(row["released"]) if row else 0

        return self._run_with_db_retry("release", _release)

    def cycle_close(self, as_of: date) -> dict[str, Any]:
        def _close() -> dict[str, Any]:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(CYCLE_CLOSE_SQL, {"as_of": as_of})
                row = cur.fetchone()
            self._conn.commit()
            return dict(row["cycle"]) if row and row["cycle"] else {}

        return self._run_with_db_retry("cycle_close", _close)
