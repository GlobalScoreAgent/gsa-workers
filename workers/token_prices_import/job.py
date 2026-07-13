#!/usr/bin/env python3
"""Enrich unpriced wallet_token_positions via DexScreener → CoinGecko cache."""

from __future__ import annotations

import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from coingecko import fetch_coingecko_prices
from db import Database
from dexscreener import fetch_dex_prices

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("token_prices_import")

DEFAULT_TTL_HOURS = 24
DEFAULT_MIN_LIQUIDITY_USD = 1000.0
DEFAULT_UPSERT_CHUNK = 500
DEFAULT_MAX_RUNTIME_SECONDS = 19800


def env_required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        raise ValueError(f"{name} is required")
    return value.strip()


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        value = default
    else:
        value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        value = default
    else:
        value = float(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def load_dotenv_if_present() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _chunk(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def main() -> int:
    load_dotenv_if_present()
    started = time.monotonic()

    try:
        dsn = env_required("SUPABASE_DB_URL")
        cg_key = env_required("COINGECKO_KEY")
        ttl_hours = env_int("PRICE_CACHE_TTL_HOURS", DEFAULT_TTL_HOURS, minimum=1)
        min_liq = env_float("MIN_LIQUIDITY_USD", DEFAULT_MIN_LIQUIDITY_USD, minimum=0.0)
        upsert_chunk = env_int("UPSERT_CHUNK_SIZE", DEFAULT_UPSERT_CHUNK)
        max_runtime = env_int("MAX_RUNTIME_SECONDS", DEFAULT_MAX_RUNTIME_SECONDS)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    logger.info(
        "Starting token price enrich (ttl_h=%s, min_liq=%s, upsert_chunk=%s)",
        ttl_hours,
        min_liq,
        upsert_chunk,
    )

    db = Database(dsn)
    upsert_rows: list[dict[str, Any]] = []
    stats = {"candidates": 0, "cache_skip": 0, "dex": 0, "cg": 0, "miss": 0}

    try:
        db.connect()
        chains = db.load_chains()
        candidates = db.load_candidates()
        fresh = db.load_fresh_cache(ttl_hours)
        stats["candidates"] = len(candidates)

        need: list[dict[str, Any]] = []
        for row in candidates:
            key = (int(row["chain_id"]), str(row["contract_address"]).lower())
            if key in fresh:
                stats["cache_skip"] += 1
                continue
            need.append(row)

        logger.info(
            "Candidates=%s cache_fresh_skip=%s to_fetch=%s chains=%s",
            stats["candidates"],
            stats["cache_skip"],
            len(need),
            len(chains),
        )

        # Group by chain_id
        by_chain: dict[int, list[dict[str, Any]]] = {}
        for row in need:
            by_chain.setdefault(int(row["chain_id"]), []).append(row)

        with httpx.Client(
            headers={"User-Agent": "gsa-workers/token_prices_import"},
            timeout=30.0,
        ) as client:
            for chain_id, rows in by_chain.items():
                if time.monotonic() - started >= max_runtime:
                    logger.warning("Max runtime reached; stopping fetch early")
                    break

                meta = chains.get(chain_id) or {}
                dex_slug = meta.get("subdomain_dexscreener")
                cg_slug = meta.get("subdomain_coingecko")
                contracts = [str(r["contract_address"]).lower() for r in rows]
                symbol_by = {
                    str(r["contract_address"]).lower(): r.get("symbol") for r in rows
                }

                dex_hits: dict[str, dict[str, Any]] = {}
                if dex_slug:
                    dex_hits = fetch_dex_prices(
                        client,
                        contracts=contracts,
                        dex_chain_id=str(dex_slug),
                        min_liquidity_usd=min_liq,
                    )

                remaining = [c for c in contracts if c not in dex_hits]
                cg_hits: dict[str, float] = {}
                if remaining and cg_slug:
                    cg_hits = fetch_coingecko_prices(
                        client,
                        api_key=cg_key,
                        platform=str(cg_slug),
                        contracts=remaining,
                    )

                for contract in contracts:
                    if contract in dex_hits:
                        hit = dex_hits[contract]
                        upsert_rows.append(
                            {
                                "chain_id": chain_id,
                                "contract_address": contract,
                                "symbol": hit.get("symbol") or symbol_by.get(contract),
                                "price_usd": hit["price_usd"],
                                "source": "dexscreener",
                                "liquidity_usd": hit.get("liquidity_usd"),
                            }
                        )
                        stats["dex"] += 1
                    elif contract in cg_hits:
                        upsert_rows.append(
                            {
                                "chain_id": chain_id,
                                "contract_address": contract,
                                "symbol": symbol_by.get(contract),
                                "price_usd": cg_hits[contract],
                                "source": "coingecko",
                                "liquidity_usd": None,
                            }
                        )
                        stats["cg"] += 1
                    else:
                        upsert_rows.append(
                            {
                                "chain_id": chain_id,
                                "contract_address": contract,
                                "symbol": symbol_by.get(contract),
                                "price_usd": None,
                                "source": "miss",
                                "liquidity_usd": None,
                            }
                        )
                        stats["miss"] += 1

                logger.info(
                    "chain_id=%s fetched=%s dex=%s cg=%s miss_so_far=%s",
                    chain_id,
                    len(contracts),
                    len(dex_hits),
                    len(cg_hits),
                    stats["miss"],
                )

        for chunk in _chunk(upsert_rows, upsert_chunk):
            if not chunk:
                continue
            # JSON null for price_usd: omit empty string — sanitize keeps None → null
            message = db.upsert_token_prices(chunk)
            logger.info("%s", message)

        apply_msg = db.apply_prices()
        logger.info("%s", apply_msg)

    except Exception:
        logger.error("Token price enrich failed:\n%s", traceback.format_exc())
        return 1
    finally:
        db.close()

    elapsed = time.monotonic() - started
    logger.info(
        "Done in %.1fs — candidates=%s cache_skip=%s dex=%s cg=%s miss=%s upserted=%s",
        elapsed,
        stats["candidates"],
        stats["cache_skip"],
        stats["dex"],
        stats["cg"],
        stats["miss"],
        len(upsert_rows),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
