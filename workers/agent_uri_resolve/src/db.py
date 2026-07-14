"""Postgres access for agent_uri_resolve."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any, TypeVar

import psycopg
from psycopg.rows import dict_row

from documents import LOOKUP_SQL, TOUCH_SQL, UPSERT_SQL, dumps_document, uri_hash

logger = logging.getLogger("agent_uri_resolve.db")

CLAIM_MAX_ATTEMPTS = 3
CLAIM_RETRY_BASE_SECONDS = 2.0
ERROR_MESSAGE_MAX_LEN = 2000
RETRYABLE_DB_EXCEPTIONS = (psycopg.OperationalError, psycopg.InterfaceError)
_NO_RECONNECT_EXCEPTIONS = (
    psycopg.errors.QueryCanceled,
    psycopg.errors.DeadlockDetected,
)

T = TypeVar("T")

CLAIM_AGENTS_SQL = """
WITH candidates AS (
  SELECT a.id
  FROM erc_8004.agents a
  WHERE a.is_uri_processed = false
    AND a.agent_uri_raw IS NOT NULL
    AND a.agent_uri_raw <> ''
  ORDER BY a.id
  LIMIT %(limit)s
  FOR UPDATE SKIP LOCKED
),
updated AS (
  UPDATE erc_8004.agents a
  SET is_uri_processed = TRUE, uri_processed_at = NOW()
  FROM candidates c
  WHERE a.id = c.id
  RETURNING a.id, a.agent_uri_raw
)
SELECT id, agent_uri_raw FROM updated
"""

CLAIM_FEEDBACKS_SQL = """
WITH candidates AS (
  SELECT rf.id
  FROM erc_8004.registration_feedbacks rf
  WHERE rf.is_feedback_processed = false
    AND rf.feedback_type IN ('feedback_uri', 'feedback_end_point')
    AND rf.agent_id IS NOT NULL
  ORDER BY rf.id
  LIMIT %(limit)s
  FOR UPDATE OF rf SKIP LOCKED
),
updated AS (
  UPDATE erc_8004.registration_feedbacks rf
  SET is_feedback_processed = TRUE, last_updated_at = NOW()
  FROM candidates c
  WHERE rf.id = c.id
  RETURNING
    rf.id,
    rf.agent_id,
    rf.feedback_type,
    rf.feedback_uri_raw,
    rf.end_point,
    rf.is_revoked,
    rf.revoked_at,
    rf.on_chain_created_at
)
SELECT * FROM updated
"""

MARK_AGENT_DONE_SQL = """
UPDATE erc_8004.agents
SET is_uri_processed = TRUE, uri_processed_at = NOW()
WHERE id = %(agent_id)s
"""

MARK_FEEDBACK_DONE_SQL = """
UPDATE erc_8004.registration_feedbacks
SET is_feedback_processed = TRUE, last_updated_at = NOW()
WHERE id = %(feedback_id)s
"""

UPSERT_MANIFEST_SQL = """
INSERT INTO erc_8004.agent_manifest AS m (
  agent_id,
  provider,
  uri_document_id,
  source,
  is_revoke,
  revoke_at,
  feedback_created_at,
  detected_at,
  updated_at,
  processed_type,
  is_active,
  url_type,
  has_download_error,
  download_error_message,
  is_processed,
  is_processed_with_error,
  processed_error_message,
  is_processed_missing_function
) VALUES (
  %(agent_id)s,
  %(provider)s,
  %(uri_document_id)s,
  %(source)s,
  %(is_revoke)s,
  %(revoke_at)s,
  %(feedback_created_at)s,
  NOW(),
  NOW(),
  %(processed_type)s,
  %(is_active)s,
  %(url_type)s,
  %(has_download_error)s,
  %(download_error_message)s,
  FALSE,
  FALSE,
  NULL,
  FALSE
)
ON CONFLICT (agent_id, provider) DO UPDATE SET
  uri_document_id = EXCLUDED.uri_document_id,
  source = EXCLUDED.source,
  is_revoke = COALESCE(EXCLUDED.is_revoke, m.is_revoke),
  revoke_at = COALESCE(EXCLUDED.revoke_at, m.revoke_at),
  feedback_created_at = COALESCE(EXCLUDED.feedback_created_at, m.feedback_created_at),
  updated_at = NOW(),
  processed_type = EXCLUDED.processed_type,
  is_active = EXCLUDED.is_active,
  url_type = EXCLUDED.url_type,
  has_download_error = EXCLUDED.has_download_error,
  download_error_message = EXCLUDED.download_error_message,
  is_processed = FALSE,
  is_processed_with_error = FALSE,
  processed_error_message = NULL,
  is_processed_missing_function = FALSE
RETURNING id
"""

ENDPOINT_URI_RE = re.compile(r"https?://[^\s,]+|ipfs://[^\s,]+", re.I)


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
                        "%s attempt %s/%s retryable (%s); sleep %.1fs",
                        operation,
                        attempt,
                        CLAIM_MAX_ATTEMPTS,
                        exc.__class__.__name__,
                        delay,
                    )
                    time.sleep(delay)
                else:
                    logger.warning(
                        "%s attempt %s/%s connection error; reconnect in %.1fs",
                        operation,
                        attempt,
                        CLAIM_MAX_ATTEMPTS,
                        delay,
                    )
                    time.sleep(delay)
                    self._reconnect()
            except Exception:
                self._safe_rollback()
                raise
        assert last_exc is not None
        raise last_exc

    def claim_agents(self, limit: int) -> list[dict[str, Any]]:
        def _claim() -> list[dict[str, Any]]:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(CLAIM_AGENTS_SQL, {"limit": limit})
                rows = list(cur.fetchall())
            self._conn.commit()
            return rows

        return self._run_with_db_retry("claim_agents", _claim)

    def claim_feedbacks(self, limit: int) -> list[dict[str, Any]]:
        def _claim() -> list[dict[str, Any]]:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(CLAIM_FEEDBACKS_SQL, {"limit": limit})
                rows = list(cur.fetchall())
            self._conn.commit()
            return rows

        return self._run_with_db_retry("claim_feedbacks", _claim)

    def lookup_document(self, uri: str) -> dict[str, Any] | None:
        def _lookup() -> dict[str, Any] | None:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(LOOKUP_SQL, {"uri_hash": uri_hash(uri)})
                row = cur.fetchone()
            self._conn.commit()
            return dict(row) if row else None

        return self._run_with_db_retry("lookup_document", _lookup)

    def touch_document(self, doc_id: int) -> None:
        def _touch() -> None:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(TOUCH_SQL, {"id": doc_id})
            self._conn.commit()

        self._run_with_db_retry("touch_document", _touch)

    def upsert_document(
        self,
        *,
        uri: str,
        document: Any,
        source_gateway: str,
        cid: str | None,
    ) -> int:
        def _upsert() -> int:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(
                    UPSERT_SQL,
                    {
                        "uri_hash": uri_hash(uri),
                        "uri": uri,
                        "cid": cid,
                        "document": dumps_document(document),
                        "source_gateway": source_gateway,
                    },
                )
                row = cur.fetchone()
            self._conn.commit()
            assert row is not None
            return int(row["id"])

        return self._run_with_db_retry("upsert_document", _upsert)

    def upsert_manifest(self, fields: dict[str, Any]) -> int:
        payload = dict(fields)
        msg = payload.get("download_error_message")
        if isinstance(msg, str) and len(msg) > ERROR_MESSAGE_MAX_LEN:
            payload["download_error_message"] = msg[: ERROR_MESSAGE_MAX_LEN - 3] + "..."

        def _upsert() -> int:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(UPSERT_MANIFEST_SQL, payload)
                row = cur.fetchone()
            self._conn.commit()
            assert row is not None
            return int(row["id"])

        return self._run_with_db_retry("upsert_manifest", _upsert)

    def mark_agent_done(self, agent_id: int) -> None:
        def _mark() -> None:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(MARK_AGENT_DONE_SQL, {"agent_id": agent_id})
            self._conn.commit()

        self._run_with_db_retry("mark_agent_done", _mark)

    def mark_feedback_done(self, feedback_id: int) -> None:
        def _mark() -> None:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(MARK_FEEDBACK_DONE_SQL, {"feedback_id": feedback_id})
            self._conn.commit()

        self._run_with_db_retry("mark_feedback_done", _mark)


def feedback_uri_and_source(row: dict[str, Any]) -> tuple[str | None, str]:
    if row.get("feedback_type") == "feedback_uri":
        uri = (row.get("feedback_uri_raw") or "").strip() or None
        return uri, "uri"
    end_point = row.get("end_point") or ""
    match = ENDPOINT_URI_RE.search(end_point)
    uri = match.group(0) if match else None
    source = ENDPOINT_URI_RE.sub("", end_point).strip() or "end_point"
    return uri, source


def processed_type_for_uri(uri: str) -> str:
    if re.match(r"^https?://", uri, flags=re.I):
        return "external_url"
    return "erc_standard"
