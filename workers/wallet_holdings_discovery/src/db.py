"""Supabase Postgres access for wallet_holdings_discovery job."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any, Literal, TypeVar

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("wallet_holdings_discovery")

Stage = Literal["contracts", "portfolio", "lp"]

CLAIM_ROWS_SQL = """
WITH candidates AS (
  SELECT wt.id
  FROM erc_8004.wallet_transactions wt
  JOIN erc_8004.chains c ON c.id = wt.chain_id
  WHERE c.subdomain_alchemy IS NOT NULL
    AND btrim(c.subdomain_alchemy) <> ''
    AND (
      (
        wt.does_need_discovery_contracts IS DISTINCT FROM FALSE
        AND (
          wt.discovery_contracts_claimed_at IS NULL
          OR wt.discovery_contracts_claimed_at
               < NOW() - make_interval(secs => %(stale_seconds)s)
        )
      )
      OR (
        wt.does_need_portfolio_discovery IS DISTINCT FROM FALSE
        AND wt.does_need_discovery_contracts = FALSE
        AND COALESCE(wt.has_discovery_contracts_error, FALSE) IS NOT TRUE
        AND (
          wt.portfolio_discovery_claimed_at IS NULL
          OR wt.portfolio_discovery_claimed_at
               < NOW() - make_interval(secs => %(stale_seconds)s)
        )
      )
      OR (
        wt.does_need_lp_discovery IS DISTINCT FROM FALSE
        AND wt.does_need_portfolio_discovery = FALSE
        AND COALESCE(wt.has_portfolio_discovery_error, FALSE) IS NOT TRUE
        AND (
          wt.lp_discovery_claimed_at IS NULL
          OR wt.lp_discovery_claimed_at
               < NOW() - make_interval(secs => %(stale_seconds)s)
        )
      )
    )
  ORDER BY
    CASE
      WHEN wt.does_need_discovery_contracts IS DISTINCT FROM FALSE THEN 0
      WHEN wt.does_need_portfolio_discovery IS DISTINCT FROM FALSE THEN 1
      ELSE 2
    END,
    wt.id
  LIMIT %(limit)s
  FOR UPDATE OF wt SKIP LOCKED
),
updated AS (
  UPDATE erc_8004.wallet_transactions wt
  SET
    discovery_contracts_claimed_at = NOW(),
    discovery_contracts_claimed_by = %(worker_id)s,
    portfolio_discovery_claimed_at = NOW(),
    portfolio_discovery_claimed_by = %(worker_id)s,
    lp_discovery_claimed_at = NOW(),
    lp_discovery_claimed_by = %(worker_id)s
  FROM candidates c
  WHERE wt.id = c.id
  RETURNING
    wt.id,
    wt.wallet_id,
    wt.chain_id,
    wt.does_need_discovery_contracts,
    wt.has_discovery_contracts_error,
    wt.does_need_portfolio_discovery,
    wt.has_portfolio_discovery_error,
    wt.does_need_lp_discovery
)
SELECT
  u.id,
  u.wallet_id,
  u.chain_id,
  u.does_need_discovery_contracts,
  u.has_discovery_contracts_error,
  u.does_need_portfolio_discovery,
  u.has_portfolio_discovery_error,
  u.does_need_lp_discovery,
  w.address,
  ch.subdomain_alchemy
FROM updated u
JOIN erc_8004.wallets w ON w.id = u.wallet_id
JOIN erc_8004.chains ch ON ch.id = u.chain_id
"""

LOAD_CONTRACTS_SQL = """
SELECT contract_address
FROM wallets.wallet_token_contracts
WHERE wallet_id = %(wallet_id)s
  AND chain_id = %(chain_id)s
ORDER BY contract_address
"""

LOAD_LP_POOLS_SQL = """
SELECT
  chain_id,
  pool_address,
  protocol,
  token0_address,
  token1_address,
  gauge_address,
  label
FROM wallets.lp_pools
WHERE chain_id = %(chain_id)s
  AND active IS TRUE
ORDER BY pool_address
"""

LOAD_TOKEN_PRICES_SQL = """
SELECT lower(contract_address) AS contract_address, price_usd
FROM wallets.token_prices
WHERE chain_id = %(chain_id)s
  AND lower(contract_address) = ANY(%(contracts)s::text[])
  AND price_usd IS NOT NULL
  AND price_usd > 0
"""

MARK_CONTRACTS_DONE_SQL = """
UPDATE erc_8004.wallet_transactions
SET
  does_need_discovery_contracts = FALSE,
  discovery_contracts_claimed_at = NOW(),
  has_discovery_contracts_error = FALSE,
  discovery_contracts_message_error = NULL
WHERE id = %(row_id)s
"""

MARK_PORTFOLIO_DONE_SQL = """
UPDATE erc_8004.wallet_transactions
SET
  does_need_portfolio_discovery = FALSE,
  portfolio_discovery_claimed_at = NOW(),
  has_portfolio_discovery_error = FALSE,
  portfolio_discovery_message_error = NULL
WHERE id = %(row_id)s
"""

MARK_LP_DONE_SQL = """
UPDATE erc_8004.wallet_transactions
SET
  does_need_lp_discovery = FALSE,
  lp_discovery_claimed_at = NOW(),
  has_lp_discovery_error = FALSE,
  lp_discovery_message_error = NULL
WHERE id = %(row_id)s
"""

MARK_CONTRACTS_ERROR_SQL = """
UPDATE erc_8004.wallet_transactions
SET
  does_need_discovery_contracts = FALSE,
  discovery_contracts_claimed_at = NOW(),
  has_discovery_contracts_error = TRUE,
  discovery_contracts_message_error = %(error_message)s
WHERE id = %(row_id)s
"""

MARK_PORTFOLIO_ERROR_SQL = """
UPDATE erc_8004.wallet_transactions
SET
  does_need_portfolio_discovery = FALSE,
  portfolio_discovery_claimed_at = NOW(),
  has_portfolio_discovery_error = TRUE,
  portfolio_discovery_message_error = %(error_message)s
WHERE id = %(row_id)s
"""

MARK_LP_ERROR_SQL = """
UPDATE erc_8004.wallet_transactions
SET
  does_need_lp_discovery = FALSE,
  lp_discovery_claimed_at = NOW(),
  has_lp_discovery_error = TRUE,
  lp_discovery_message_error = %(error_message)s
WHERE id = %(row_id)s
"""

RELEASE_CONTRACTS_TRANSIENT_SQL = """
UPDATE erc_8004.wallet_transactions
SET discovery_contracts_claimed_at = NOW()
WHERE id = %(row_id)s
"""

RELEASE_PORTFOLIO_TRANSIENT_SQL = """
UPDATE erc_8004.wallet_transactions
SET portfolio_discovery_claimed_at = NOW()
WHERE id = %(row_id)s
"""

RELEASE_LP_TRANSIENT_SQL = """
UPDATE erc_8004.wallet_transactions
SET lp_discovery_claimed_at = NOW()
WHERE id = %(row_id)s
"""

UPSERT_CONTRACTS_SQL = """
SELECT wallets.wallet_token_contracts_upsert(
  %(wallet_id)s,
  %(chain_id)s,
  %(rows)s::jsonb
)
"""

INSERT_POSITIONS_SQL = """
SELECT wallets.wallet_token_positions_insert(
  %(wallet_id)s,
  %(chain_id)s,
  %(rows)s::jsonb
)
"""

UPSERT_LP_SQL = """
SELECT wallets.wallet_lp_positions_upsert(
  %(wallet_id)s,
  %(chain_id)s,
  %(rows)s::jsonb
)
"""

_MARK_ERROR_SQL: dict[Stage, str] = {
    "contracts": MARK_CONTRACTS_ERROR_SQL,
    "portfolio": MARK_PORTFOLIO_ERROR_SQL,
    "lp": MARK_LP_ERROR_SQL,
}

_RELEASE_TRANSIENT_SQL: dict[Stage, str] = {
    "contracts": RELEASE_CONTRACTS_TRANSIENT_SQL,
    "portfolio": RELEASE_PORTFOLIO_TRANSIENT_SQL,
    "lp": RELEASE_LP_TRANSIENT_SQL,
}

CLAIM_MAX_ATTEMPTS = 3
CLAIM_RETRY_BASE_SECONDS = 2.0
ERROR_MESSAGE_MAX_LEN = 2000
RETRYABLE_DB_EXCEPTIONS = (psycopg.OperationalError, psycopg.InterfaceError)
_NO_RECONNECT_EXCEPTIONS = (
    psycopg.errors.QueryCanceled,
    psycopg.errors.DeadlockDetected,
)

T = TypeVar("T")


def _rpc_message(result: dict[str, Any] | None) -> str:
    if result is None:
        return ""
    return str(next(iter(result.values())))


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
                    CLAIM_ROWS_SQL,
                    {
                        "worker_id": worker_id,
                        "limit": limit,
                        "stale_seconds": stale_seconds,
                    },
                )
                rows = list(cur.fetchall())
            self._conn.commit()
            return rows

        return self._run_with_db_retry("claim", _claim)

    def load_contracts(self, wallet_id: int, chain_id: int) -> list[str]:
        def _load() -> list[str]:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(
                    LOAD_CONTRACTS_SQL,
                    {"wallet_id": wallet_id, "chain_id": chain_id},
                )
                return [str(r["contract_address"]).lower() for r in cur.fetchall()]

        return self._run_with_db_retry("load_contracts", _load)

    def load_lp_pools(self, chain_id: int) -> list[dict[str, Any]]:
        def _load() -> list[dict[str, Any]]:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(LOAD_LP_POOLS_SQL, {"chain_id": chain_id})
                return list(cur.fetchall())

        return self._run_with_db_retry("load_lp_pools", _load)

    def load_token_prices(
        self, chain_id: int, contracts: list[str]
    ) -> dict[str, float]:
        if not contracts:
            return {}

        def _load() -> dict[str, float]:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(
                    LOAD_TOKEN_PRICES_SQL,
                    {
                        "chain_id": chain_id,
                        "contracts": [c.lower() for c in contracts],
                    },
                )
                out: dict[str, float] = {}
                for row in cur.fetchall():
                    try:
                        out[str(row["contract_address"]).lower()] = float(
                            row["price_usd"]
                        )
                    except (TypeError, ValueError, KeyError):
                        continue
                return out

        return self._run_with_db_retry("load_token_prices", _load)

    def upsert_contracts_and_mark_done(
        self,
        row_id: int,
        wallet_id: int,
        chain_id: int,
        contracts: list[dict[str, str]],
    ) -> str:
        def _save() -> str:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(
                    UPSERT_CONTRACTS_SQL,
                    {
                        "wallet_id": wallet_id,
                        "chain_id": chain_id,
                        "rows": json.dumps(contracts),
                    },
                )
                result = cur.fetchone()
                cur.execute(MARK_CONTRACTS_DONE_SQL, {"row_id": row_id})
            self._conn.commit()
            return _rpc_message(result)

        return self._run_with_db_retry("upsert_contracts_and_mark_done", _save)

    def insert_positions_and_mark_done(
        self,
        row_id: int,
        wallet_id: int,
        chain_id: int,
        positions: list[dict[str, Any]],
    ) -> str:
        def _save() -> str:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(
                    INSERT_POSITIONS_SQL,
                    {
                        "wallet_id": wallet_id,
                        "chain_id": chain_id,
                        "rows": json.dumps(positions),
                    },
                )
                result = cur.fetchone()
                cur.execute(MARK_PORTFOLIO_DONE_SQL, {"row_id": row_id})
            self._conn.commit()
            return _rpc_message(result)

        return self._run_with_db_retry("insert_positions_and_mark_done", _save)

    def upsert_lp_and_mark_done(
        self,
        row_id: int,
        wallet_id: int,
        chain_id: int,
        positions: list[dict[str, Any]],
    ) -> str:
        def _save() -> str:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(
                    UPSERT_LP_SQL,
                    {
                        "wallet_id": wallet_id,
                        "chain_id": chain_id,
                        "rows": json.dumps(positions),
                    },
                )
                result = cur.fetchone()
                cur.execute(MARK_LP_DONE_SQL, {"row_id": row_id})
            self._conn.commit()
            return _rpc_message(result)

        return self._run_with_db_retry("upsert_lp_and_mark_done", _save)

    def mark_error(self, row_id: int, stage: Stage, error_message: str) -> None:
        msg = (error_message or "unknown error").strip()
        if len(msg) > ERROR_MESSAGE_MAX_LEN:
            msg = msg[: ERROR_MESSAGE_MAX_LEN - 3] + "..."
        sql = _MARK_ERROR_SQL[stage]

        def _mark() -> None:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(sql, {"row_id": row_id, "error_message": msg})
            self._conn.commit()

        self._run_with_db_retry(f"mark_error_{stage}", _mark)

    def release_transient(self, row_id: int, stage: Stage) -> None:
        """Keep the stage flag pending; stamp claimed_at so the stale window applies."""
        sql = _RELEASE_TRANSIENT_SQL[stage]

        def _release() -> None:
            assert self._conn is not None
            with self._conn.cursor() as cur:
                cur.execute(sql, {"row_id": row_id})
            self._conn.commit()

        self._run_with_db_retry(f"release_transient_{stage}", _release)
