"""Supabase Postgres access for ethos_enrich job."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("ethos_enrich")

CLAIM_MAX_ATTEMPTS = 3
CLAIM_RETRY_BASE_SECONDS = 2.0
RETRYABLE_DB_EXCEPTIONS = (psycopg.OperationalError, psycopg.InterfaceError)
_NO_RECONNECT_EXCEPTIONS = (
    psycopg.errors.QueryCanceled,
    psycopg.errors.DeadlockDetected,
)

T = TypeVar("T")

# Upsert SQL keyed by entity name (matches ethos.* tables / normalize columns)
UPSERT_SQL: dict[str, str] = {
    "attestations": """
INSERT INTO ethos.attestations (
  graph_id, attestation_id, profile_id, service, account, evidence,
  created_at_on_chain, archived, imported_at, updated_at
) VALUES (
  %(graph_id)s, %(attestation_id)s, %(profile_id)s, %(service)s, %(account)s,
  %(evidence)s, to_timestamp(%(created_at)s), %(archived)s, NOW(), NOW()
)
ON CONFLICT (graph_id) DO UPDATE SET
  attestation_id = EXCLUDED.attestation_id, profile_id = EXCLUDED.profile_id,
  service = EXCLUDED.service, account = EXCLUDED.account, evidence = EXCLUDED.evidence,
  created_at_on_chain = EXCLUDED.created_at_on_chain, archived = EXCLUDED.archived,
  updated_at = NOW()
""",
    "reviews": """
INSERT INTO ethos.reviews (
  graph_id, review_id, score, author_address, subject_address, attestation_hash,
  comment, metadata, created_at_on_chain, archived,
  author_profile_id, subject_profile_id, imported_at, updated_at
) VALUES (
  %(graph_id)s, %(review_id)s, %(score)s, %(author_address)s, %(subject_address)s,
  %(attestation_hash)s, %(comment)s, %(metadata)s, to_timestamp(%(created_at)s),
  %(archived)s, %(author_profile_id)s, %(subject_profile_id)s, NOW(), NOW()
)
ON CONFLICT (graph_id) DO UPDATE SET
  review_id = EXCLUDED.review_id, score = EXCLUDED.score,
  author_address = EXCLUDED.author_address, subject_address = EXCLUDED.subject_address,
  attestation_hash = EXCLUDED.attestation_hash, comment = EXCLUDED.comment,
  metadata = EXCLUDED.metadata, created_at_on_chain = EXCLUDED.created_at_on_chain,
  archived = EXCLUDED.archived, author_profile_id = EXCLUDED.author_profile_id,
  subject_profile_id = EXCLUDED.subject_profile_id, updated_at = NOW()
""",
    "vouches": """
INSERT INTO ethos.vouches (
  graph_id, vouch_id, balance, archived, unhealthy, vouched_at, unvouched_at,
  comment, metadata, author_profile_id, subject_profile_id, imported_at, updated_at
) VALUES (
  %(graph_id)s, %(vouch_id)s, %(balance)s, %(archived)s, %(unhealthy)s,
  to_timestamp(%(vouched_at)s), to_timestamp(%(unvouched_at)s),
  %(comment)s, %(metadata)s, %(author_profile_id)s, %(subject_profile_id)s, NOW(), NOW()
)
ON CONFLICT (graph_id) DO UPDATE SET
  vouch_id = EXCLUDED.vouch_id, balance = EXCLUDED.balance, archived = EXCLUDED.archived,
  unhealthy = EXCLUDED.unhealthy, vouched_at = EXCLUDED.vouched_at,
  unvouched_at = EXCLUDED.unvouched_at, comment = EXCLUDED.comment,
  metadata = EXCLUDED.metadata, author_profile_id = EXCLUDED.author_profile_id,
  subject_profile_id = EXCLUDED.subject_profile_id, updated_at = NOW()
""",
    "slashes": """
INSERT INTO ethos.slashes (
  graph_id, slash_id, amount, created_at_on_chain, archived, slash_type,
  comment, metadata, subject_address, attestation_hash,
  author_profile_id, subject_profile_id, imported_at, updated_at
) VALUES (
  %(graph_id)s, %(slash_id)s, %(amount)s, to_timestamp(%(created_at)s), %(archived)s,
  %(slash_type)s, %(comment)s, %(metadata)s, %(subject_address)s, %(attestation_hash)s,
  %(author_profile_id)s, %(subject_profile_id)s, NOW(), NOW()
)
ON CONFLICT (graph_id) DO UPDATE SET
  slash_id = EXCLUDED.slash_id, amount = EXCLUDED.amount,
  created_at_on_chain = EXCLUDED.created_at_on_chain, archived = EXCLUDED.archived,
  slash_type = EXCLUDED.slash_type, comment = EXCLUDED.comment, metadata = EXCLUDED.metadata,
  subject_address = EXCLUDED.subject_address, attestation_hash = EXCLUDED.attestation_hash,
  author_profile_id = EXCLUDED.author_profile_id, subject_profile_id = EXCLUDED.subject_profile_id,
  updated_at = NOW()
""",
    "reputation_markets": """
INSERT INTO ethos.reputation_markets (
  graph_id, profile_id, graduated, vote_trust, vote_distrust,
  trust_price, distrust_price, liquidity, base_price,
  created_at_on_chain, updated_at_on_chain, imported_at, updated_at
) VALUES (
  %(graph_id)s, %(profile_id)s, %(graduated)s, %(vote_trust)s, %(vote_distrust)s,
  %(trust_price)s, %(distrust_price)s, %(liquidity)s, %(base_price)s,
  to_timestamp(%(created_at)s), to_timestamp(%(updated_at)s), NOW(), NOW()
)
ON CONFLICT (graph_id) DO UPDATE SET
  profile_id = EXCLUDED.profile_id, graduated = EXCLUDED.graduated,
  vote_trust = EXCLUDED.vote_trust, vote_distrust = EXCLUDED.vote_distrust,
  trust_price = EXCLUDED.trust_price, distrust_price = EXCLUDED.distrust_price,
  liquidity = EXCLUDED.liquidity, base_price = EXCLUDED.base_price,
  created_at_on_chain = EXCLUDED.created_at_on_chain,
  updated_at_on_chain = EXCLUDED.updated_at_on_chain, updated_at = NOW()
""",
    "market_trades": """
INSERT INTO ethos.market_trades (
  graph_id, profile_id, trader_address, is_positive, is_buy,
  amount, funds, traded_at, tx_hash, imported_at, updated_at
) VALUES (
  %(graph_id)s, %(profile_id)s, %(trader_address)s, %(is_positive)s, %(is_buy)s,
  %(amount)s, %(funds)s, to_timestamp(%(traded_at)s), %(tx_hash)s, NOW(), NOW()
)
ON CONFLICT (graph_id) DO UPDATE SET
  profile_id = EXCLUDED.profile_id, trader_address = EXCLUDED.trader_address,
  is_positive = EXCLUDED.is_positive, is_buy = EXCLUDED.is_buy,
  amount = EXCLUDED.amount, funds = EXCLUDED.funds, traded_at = EXCLUDED.traded_at,
  tx_hash = EXCLUDED.tx_hash, updated_at = NOW()
""",
    "broker_posts": """
INSERT INTO ethos.broker_posts (
  graph_id, post_id, author_profile_id, post_type, title, description,
  cost, tags, level, created_at_on_chain, updated_at_on_chain, tx_hash,
  imported_at, updated_at
) VALUES (
  %(graph_id)s, %(post_id)s, %(author_profile_id)s, %(post_type)s, %(title)s,
  %(description)s, %(cost)s, %(tags)s, %(level)s, to_timestamp(%(created_at)s),
  to_timestamp(%(updated_at)s), %(tx_hash)s, NOW(), NOW()
)
ON CONFLICT (graph_id) DO UPDATE SET
  post_id = EXCLUDED.post_id, author_profile_id = EXCLUDED.author_profile_id,
  post_type = EXCLUDED.post_type, title = EXCLUDED.title, description = EXCLUDED.description,
  cost = EXCLUDED.cost, tags = EXCLUDED.tags, level = EXCLUDED.level,
  created_at_on_chain = EXCLUDED.created_at_on_chain,
  updated_at_on_chain = EXCLUDED.updated_at_on_chain, tx_hash = EXCLUDED.tx_hash,
  updated_at = NOW()
""",
    "projects": """
INSERT INTO ethos.projects (
  graph_id, project_id, userkey, status, name, description,
  created_at_on_chain, updated_at_on_chain, owner_profile_id, imported_at, updated_at
) VALUES (
  %(graph_id)s, %(project_id)s, %(userkey)s, %(status)s, %(name)s, %(description)s,
  to_timestamp(%(created_at)s), to_timestamp(%(updated_at)s),
  %(owner_profile_id)s, NOW(), NOW()
)
ON CONFLICT (graph_id) DO UPDATE SET
  project_id = EXCLUDED.project_id, userkey = EXCLUDED.userkey, status = EXCLUDED.status,
  name = EXCLUDED.name, description = EXCLUDED.description,
  created_at_on_chain = EXCLUDED.created_at_on_chain,
  updated_at_on_chain = EXCLUDED.updated_at_on_chain,
  owner_profile_id = EXCLUDED.owner_profile_id, updated_at = NOW()
""",
    "bonds": """
INSERT INTO ethos.bonds (
  graph_id, bond_id, amount, bond_type, amount_type, status,
  created_at_on_chain, released_at, author_profile_id, imported_at, updated_at
) VALUES (
  %(graph_id)s, %(bond_id)s, %(amount)s, %(bond_type)s, %(amount_type)s, %(status)s,
  to_timestamp(%(created_at)s), to_timestamp(%(released_at)s),
  %(author_profile_id)s, NOW(), NOW()
)
ON CONFLICT (graph_id) DO UPDATE SET
  bond_id = EXCLUDED.bond_id, amount = EXCLUDED.amount, bond_type = EXCLUDED.bond_type,
  amount_type = EXCLUDED.amount_type, status = EXCLUDED.status,
  created_at_on_chain = EXCLUDED.created_at_on_chain, released_at = EXCLUDED.released_at,
  author_profile_id = EXCLUDED.author_profile_id, updated_at = NOW()
""",
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

    def claim_history(self, worker_id: str, limit: int, stale_seconds: int) -> list[int]:
        def _claim() -> list[int]:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT profile_id FROM ethos.claim_history_fetch(%s, %s, %s)",
                    (limit, worker_id, stale_seconds),
                )
                rows = list(cur.fetchall())
            self._conn.commit()
            return [int(r["profile_id"]) for r in rows]

        return self._run_with_db_retry("claim_history", _claim)

    def complete_history(self, profile_id: int) -> None:
        def _complete() -> None:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT ethos.complete_history_fetch(%s)",
                    (profile_id,),
                )
            self._conn.commit()

        self._run_with_db_retry("complete_history", _complete)

    def upsert_entity_rows(self, entity: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        sql = UPSERT_SQL.get(entity)
        if sql is None:
            raise ValueError(f"unknown entity for upsert: {entity}")

        def _upsert() -> int:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.executemany(sql, rows)
            self._conn.commit()
            return len(rows)

        return self._run_with_db_retry(f"upsert_{entity}", _upsert)

    def list_score_candidates(self, limit: int) -> list[dict[str, Any]]:
        def _list() -> list[dict[str, Any]]:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT wallet_id, address, profile_id FROM ethos.list_score_candidates(%s)",
                    (limit,),
                )
                rows = list(cur.fetchall())
            self._conn.commit()
            return rows

        return self._run_with_db_retry("list_score_candidates", _list)

    def upsert_official_scores(self, rows: list[dict[str, Any]], ttl_days: int = 15) -> int:
        def _upsert() -> int:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT ethos.upsert_official_scores(%s::jsonb, %s)",
                    (json.dumps(rows), ttl_days),
                )
                result = cur.fetchone()
            self._conn.commit()
            if result is None:
                return 0
            return int(next(iter(result.values())))

        return self._run_with_db_retry("upsert_official_scores", _upsert)
