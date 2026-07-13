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

from coingecko import (
    DEFAULT_BATCH_SIZE as CG_BATCH_SIZE,
    CoinGeckoAuthError,
    fetch_coingecko_prices,
)
from db import Database
from dexscreener import (
    DEFAULT_BATCH_SIZE as DEX_BATCH_SIZE,
    fetch_dex_prices,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("token_prices_import")

DEFAULT_TTL_HOURS = 24
DEFAULT_MIN_LIQUIDITY_USD = 1000.0
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


def _batches(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def main() -> int:
    load_dotenv_if_present()
    started = time.monotonic()

    try:
        dsn = env_required("SUPABASE_DB_URL")
        cg_key = env_required("COINGECKO_KEY")
        cg_plan_raw = os.environ.get("COINGECKO_API_PLAN", "demo").strip().lower() or "demo"
        if cg_plan_raw not in ("demo", "pro"):
            raise ValueError("COINGECKO_API_PLAN must be demo or pro")
        cg_plan: str = cg_plan_raw
        ttl_hours = env_int("PRICE_CACHE_TTL_HOURS", DEFAULT_TTL_HOURS, minimum=1)
        min_liq = env_float("MIN_LIQUIDITY_USD", DEFAULT_MIN_LIQUIDITY_USD, minimum=0.0)
        max_runtime = env_int("MAX_RUNTIME_SECONDS", DEFAULT_MAX_RUNTIME_SECONDS)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    logger.info(
        "Starting token price enrich (ttl_h=%s, min_liq=%s, cg_plan=%s, dex_batch=%s, cg_batch=%s)",
        ttl_hours,
        min_liq,
        cg_plan,
        DEX_BATCH_SIZE,
        CG_BATCH_SIZE,
    )

    db = Database(dsn)
    stats = {
        "candidates": 0,
        "cache_skip": 0,
        "dex": 0,
        "cg": 0,
        "miss": 0,
        "upserted": 0,
    }

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
                resolved: set[str] = set()

                # Dex in batches of 30 → upsert hits immediately
                if dex_slug:
                    for dex_chunk in _batches(contracts, DEX_BATCH_SIZE):
                        if time.monotonic() - started >= max_runtime:
                            break
                        dex_hits = fetch_dex_prices(
                            client,
                            contracts=dex_chunk,
                            dex_chain_id=str(dex_slug),
                            min_liquidity_usd=min_liq,
                            batch_size=len(dex_chunk),
                        )
                        upsert_rows: list[dict[str, Any]] = []
                        for contract, hit in dex_hits.items():
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
                            resolved.add(contract)
                            stats["dex"] += 1
                        if upsert_rows:
                            msg = db.upsert_token_prices(upsert_rows)
                            stats["upserted"] += len(upsert_rows)
                            logger.info("Dex upsert chain_id=%s n=%s — %s", chain_id, len(upsert_rows), msg)

                remaining = [c for c in contracts if c not in resolved]

                # CoinGecko in batches of 100 → upsert hits immediately
                if remaining and cg_slug:
                    for cg_chunk in _batches(remaining, CG_BATCH_SIZE):
                        if time.monotonic() - started >= max_runtime:
                            break
                        try:
                            cg_hits = fetch_coingecko_prices(
                                client,
                                api_key=cg_key,
                                platform=str(cg_slug),
                                contracts=cg_chunk,
                                api_plan=cg_plan,  # type: ignore[arg-type]
                                batch_size=len(cg_chunk),
                            )
                        except CoinGeckoAuthError as exc:
                            logger.error("%s", exc)
                            return 1
                        upsert_rows = []
                        for contract, price in cg_hits.items():
                            upsert_rows.append(
                                {
                                    "chain_id": chain_id,
                                    "contract_address": contract,
                                    "symbol": symbol_by.get(contract),
                                    "price_usd": price,
                                    "source": "coingecko",
                                    "liquidity_usd": None,
                                }
                            )
                            resolved.add(contract)
                            stats["cg"] += 1
                        if upsert_rows:
                            msg = db.upsert_token_prices(upsert_rows)
                            stats["upserted"] += len(upsert_rows)
                            logger.info("CG upsert chain_id=%s n=%s — %s", chain_id, len(upsert_rows), msg)

                # Misses for this chain
                miss_contracts = [c for c in contracts if c not in resolved]
                if miss_contracts:
                    miss_rows = [
                        {
                            "chain_id": chain_id,
                            "contract_address": c,
                            "symbol": symbol_by.get(c),
                            "price_usd": None,
                            "source": "miss",
                            "liquidity_usd": None,
                        }
                        for c in miss_contracts
                    ]
                    for i in range(0, len(miss_rows), 500):
                        chunk_rows = miss_rows[i : i + 500]
                        msg = db.upsert_token_prices(chunk_rows)
                        stats["miss"] += len(chunk_rows)
                        stats["upserted"] += len(chunk_rows)
                        logger.info(
                            "Miss upsert chain_id=%s n=%s — %s",
                            chain_id,
                            len(chunk_rows),
                            msg,
                        )
                        mark_msg = db.mark_price_misses(chunk_rows)
                        logger.info(
                            "Miss mark positions chain_id=%s — %s",
                            chain_id,
                            mark_msg,
                        )

                apply_msg = db.apply_prices()
                logger.info(
                    "chain_id=%s done contracts=%s dex=%s cg=%s miss=%s — %s",
                    chain_id,
                    len(contracts),
                    stats["dex"],
                    stats["cg"],
                    stats["miss"],
                    apply_msg,
                )

        # Final apply in case cache_skip-only run or leftover
        apply_msg = db.apply_prices()
        logger.info("Final apply — %s", apply_msg)

    except CoinGeckoAuthError as exc:
        logger.error("%s", exc)
        return 1
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
        stats["upserted"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
