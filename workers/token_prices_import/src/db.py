"""Supabase Postgres access for token_prices_import enrich job."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("token_prices_import")

UPSERT_TOKEN_PRICES_SQL = """
SELECT wallets.token_prices_upsert(%(rows)s::jsonb) AS message
"""

APPLY_PRICES_SQL = """
SELECT wallets.wallet_token_positions_apply_prices() AS message
"""

MARK_PRICE_MISSES_SQL = """
SELECT wallets.wallet_token_positions_mark_price_misses(%(rows)s::jsonb) AS message
"""

LOAD_CHAINS_SQL = """
SELECT id, subdomain_coingecko, subdomain_dexscreener
FROM erc_8004.chains
WHERE subdomain_coingecko IS NOT NULL
   OR subdomain_dexscreener IS NOT NULL
"""

LOAD_CANDIDATES_SQL = """
SELECT DISTINCT ON (p.chain_id, lower(p.contract_address))
  p.chain_id,
  lower(p.contract_address) AS contract_address,
  p.symbol
FROM wallets.wallet_token_positions p
INNER JOIN erc_8004.chains c ON c.id = p.chain_id
WHERE p.has_price_error IS TRUE
  AND COALESCE(p.token_quality, '') IS DISTINCT FROM 'spam'
  AND p.contract_address <> 'native'
  AND p.contract_address ~ '^0x[0-9a-f]{40}$'
  AND (
    c.subdomain_dexscreener IS NOT NULL
    OR c.subdomain_coingecko IS NOT NULL
  )
ORDER BY p.chain_id, lower(p.contract_address), p.symbol NULLS LAST
"""

LOAD_FRESH_CACHE_SQL = """
SELECT chain_id, contract_address, price_usd, source, fetched_at
FROM wallets.token_prices
WHERE fetched_at >= now() - make_interval(hours => %(ttl_hours)s)
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

    def load_chains(self) -> dict[int, dict[str, str | None]]:
        def _load() -> dict[int, dict[str, str | None]]:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(LOAD_CHAINS_SQL)
                rows = cur.fetchall()
            out: dict[int, dict[str, str | None]] = {}
            for row in rows:
                out[int(row["id"])] = {
                    "subdomain_coingecko": row.get("subdomain_coingecko"),
                    "subdomain_dexscreener": row.get("subdomain_dexscreener"),
                }
            return out

        return self._run_with_db_retry("load_chains", _load)

    def load_candidates(self) -> list[dict[str, Any]]:
        def _load() -> list[dict[str, Any]]:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(LOAD_CANDIDATES_SQL)
                return list(cur.fetchall())

        return self._run_with_db_retry("load_candidates", _load)

    def load_fresh_cache(self, ttl_hours: int) -> set[tuple[int, str]]:
        def _load() -> set[tuple[int, str]]:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(LOAD_FRESH_CACHE_SQL, {"ttl_hours": ttl_hours})
                rows = cur.fetchall()
            return {(int(r["chain_id"]), str(r["contract_address"]).lower()) for r in rows}

        return self._run_with_db_retry("load_fresh_cache", _load)

    def upsert_token_prices(self, rows: list[dict[str, Any]]) -> str:
        cleaned = [_sanitize_row(row) for row in rows]
        payload = json.dumps(cleaned, separators=(",", ":"), ensure_ascii=True)

        def _upsert() -> str:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(UPSERT_TOKEN_PRICES_SQL, {"rows": payload})
                result = cur.fetchone()
            self._conn.commit()
            if not result or result.get("message") is None:
                raise RuntimeError("token_prices_upsert returned no message")
            return str(result["message"])

        return self._run_with_db_retry("token_prices_upsert", _upsert)

    def apply_prices(self) -> str:
        def _apply() -> str:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(APPLY_PRICES_SQL)
                result = cur.fetchone()
            self._conn.commit()
            if not result or result.get("message") is None:
                raise RuntimeError("wallet_token_positions_apply_prices returned no message")
            return str(result["message"])

        return self._run_with_db_retry("apply_prices", _apply)

    def mark_price_misses(self, rows: list[dict[str, Any]]) -> str:
        """Mark positions as known-unknown after Dex+CG miss (leave enrich queue)."""
        cleaned = [
            {
                "chain_id": int(r["chain_id"]),
                "contract_address": str(r["contract_address"]).strip().lower(),
            }
            for r in rows
        ]
        payload = json.dumps(cleaned, separators=(",", ":"), ensure_ascii=True)

        def _mark() -> str:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(MARK_PRICE_MISSES_SQL, {"rows": payload})
                result = cur.fetchone()
            self._conn.commit()
            if not result or result.get("message") is None:
                raise RuntimeError(
                    "wallet_token_positions_mark_price_misses returned no message"
                )
            return str(result["message"])

        return self._run_with_db_retry("mark_price_misses", _mark)


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {str(k).replace("\x00", ""): _sanitize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(v) for v in value]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return value


def _sanitize_row(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise TypeError(f"expected dict row, got {type(row).__name__}")
    return {str(k).replace("\x00", ""): _sanitize_value(v) for k, v in row.items()}
