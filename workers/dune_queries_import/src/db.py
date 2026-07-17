"""Supabase Postgres access for dune_queries_import job."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("dune_queries_import")

DEFAULT_UPSERT_CHUNK_SIZE = 5_000
CLAIM_MAX_ATTEMPTS = 3
CLAIM_RETRY_BASE_SECONDS = 2.0
RETRYABLE_DB_EXCEPTIONS = (psycopg.OperationalError, psycopg.InterfaceError)
_NO_RECONNECT_EXCEPTIONS = (
    psycopg.errors.QueryCanceled,
    psycopg.errors.DeadlockDetected,
)

T = TypeVar("T")


class Database:
    def __init__(self, dsn: str, *, statement_timeout: str = "600s"):
        self._dsn = dsn
        self._statement_timeout = statement_timeout
        self._conn: psycopg.Connection | None = None

    def connect(self) -> None:
        self._conn = psycopg.connect(self._dsn, row_factory=dict_row)
        with self._conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = '{self._statement_timeout}'")

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

    def upsert_rows_chunked(
        self,
        *,
        task_name: str,
        rpc_sql: str,
        rows: list[dict[str, Any]],
        chunk_size: int = DEFAULT_UPSERT_CHUNK_SIZE,
    ) -> str:
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        if not rows:
            raise ValueError("rows must not be empty")

        total = len(rows)
        chunk_count = (total + chunk_size - 1) // chunk_size
        messages: list[str] = []

        for index in range(chunk_count):
            start = index * chunk_size
            chunk = rows[start : start + chunk_size]
            payload = json.dumps(chunk, separators=(",", ":"))
            operation = f"{task_name}_upsert_chunk_{index + 1}/{chunk_count}"

            def _upsert(payload: str = payload, operation: str = operation) -> str:
                assert self._conn is not None
                with self._conn.cursor() as cur:
                    cur.execute(rpc_sql, {"rows": payload})
                    result = cur.fetchone()
                self._conn.commit()
                if not result or result.get("message") is None:
                    raise RuntimeError(f"{operation} returned no message")
                return str(result["message"])

            message = self._run_with_db_retry(operation, _upsert)
            messages.append(message)
            logger.info(
                "Task %s upsert chunk %s/%s (%s rows): %s",
                task_name,
                index + 1,
                chunk_count,
                len(chunk),
                message,
            )

        return (
            f"{task_name}: {total} rows upserted in {chunk_count} chunk(s); "
            f"last={messages[-1]}"
        )
