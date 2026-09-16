"""Postgres access for humi_reason_publisher."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

import psycopg
from psycopg.rows import dict_row

from pillar_spec import PILLARS, pillar_columns

logger = logging.getLogger("humi_reason_publisher")

CLAIM_MAX_ATTEMPTS = 3
CLAIM_RETRY_BASE_SECONDS = 2.0
RETRYABLE_DB_EXCEPTIONS = (psycopg.OperationalError, psycopg.InterfaceError)
_NO_RECONNECT_EXCEPTIONS = (
    psycopg.errors.QueryCanceled,
    psycopg.errors.DeadlockDetected,
)

CLAIM_SQL = """
SELECT agent_id, version, reason_content_sha256
FROM index_humi.claim_reason_publish(
  %(limit)s,
  %(worker_id)s,
  %(stale_seconds)s
)
"""
COMPLETE_SQL = """
SELECT index_humi.complete_reason_publish(%(rows)s::jsonb)
"""

# Una consulta por tabla de pilar, derivada de pillar_spec: si se agrega un item
# al spec, la lectura lo acompana sola.
PILLAR_SQL: dict[str, str] = {
    pillar.table: "SELECT agent_id, {cols} FROM index_humi.{table} WHERE agent_id = ANY(%(agent_ids)s)".format(
        cols=", ".join(pillar_columns(pillar)),
        table=pillar.table,
    )
    for pillar in PILLARS
}

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
                    CLAIM_SQL,
                    {
                        "limit": limit,
                        "worker_id": worker_id,
                        "stale_seconds": stale_seconds,
                    },
                )
                rows = list(cur.fetchall())
            self._conn.commit()
            return rows

        return self._run_with_db_retry("claim", _claim)

    def fetch_pillars(
        self, agent_ids: list[int]
    ) -> dict[int, dict[str, dict[str, Any] | None]]:
        """Devuelve {agent_id: {tabla_pilar: fila|None}} para todo el lote."""
        if not agent_ids:
            return {}

        def _fetch() -> dict[int, dict[str, dict[str, Any] | None]]:
            assert self._conn is not None
            by_agent: dict[int, dict[str, dict[str, Any] | None]] = {
                agent_id: {pillar.table: None for pillar in PILLARS} for agent_id in agent_ids
            }
            with self._conn.cursor() as cur:
                for table, sql in PILLAR_SQL.items():
                    cur.execute(sql, {"agent_ids": agent_ids})
                    for row in cur.fetchall():
                        agent_id = int(row.pop("agent_id"))
                        slot = by_agent.get(agent_id)
                        if slot is not None:
                            slot[table] = row
            self._conn.commit()
            return by_agent

        return self._run_with_db_retry("fetch_pillars", _fetch)

    def complete(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0

        def _complete() -> int:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(COMPLETE_SQL, {"rows": json.dumps(rows)})
                result = cur.fetchone()
            self._conn.commit()
            return int(result["complete_reason_publish"]) if result else 0

        return self._run_with_db_retry("complete", _complete)
