#!/usr/bin/env python3
"""Import CEX addresses from Dune into wallets.cex_addresses."""

from __future__ import annotations

import logging
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from db import Database
from dune import (
    DEFAULT_PAGE_DELAY_SECONDS,
    DEFAULT_PAGE_SIZE,
    DuneError,
    fetch_latest_rows,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("cex_addresses_import")

DEFAULT_QUERY_ID = 7520736


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


def main() -> int:
    load_dotenv_if_present()
    started = time.monotonic()

    try:
        dsn = env_required("SUPABASE_DB_URL")
        dune_key = env_required("DUNE_KEY")
        query_id = env_int("DUNE_QUERY_ID", DEFAULT_QUERY_ID)
        page_size = env_int("DUNE_PAGE_SIZE", DEFAULT_PAGE_SIZE)
        page_delay = env_float("DUNE_PAGE_DELAY_SECONDS", DEFAULT_PAGE_DELAY_SECONDS)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    logger.info(
        "Starting CEX addresses import (query_id=%s, page_size=%s, page_delay=%.1fs)",
        query_id,
        page_size,
        page_delay,
    )

    try:
        rows = fetch_latest_rows(
            dune_key,
            query_id,
            page_size=page_size,
            page_delay_seconds=page_delay,
        )
    except DuneError as exc:
        logger.error("Dune fetch failed: %s", exc)
        return 1
    except Exception:
        logger.error("Dune fetch failed unexpectedly:\n%s", traceback.format_exc())
        return 1

    if not rows:
        logger.error("Dune returned 0 rows; refusing to upsert empty payload")
        return 1

    logger.info("Fetched %s rows from Dune; calling wallets.cex_addresses_upsert", len(rows))

    db = Database(dsn)
    try:
        db.connect()
        message = db.upsert_cex_addresses(rows)
    except Exception:
        logger.error("Upsert failed:\n%s", traceback.format_exc())
        return 1
    finally:
        db.close()

    elapsed = time.monotonic() - started
    logger.info("Done in %.1fs — %s", elapsed, message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
