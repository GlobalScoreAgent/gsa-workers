#!/usr/bin/env python3
"""Import token prices from Dune into wallets.token_prices."""

from __future__ import annotations

import logging
import os
import re
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
logger = logging.getLogger("token_prices_import")

DEFAULT_QUERY_ID = 7526826
DEFAULT_UPSERT_CHUNK_SIZE = 5000
_INSERTED_RE = re.compile(r"(\d+)\s+rows inserted", re.IGNORECASE)


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


def _parse_inserted_count(message: str) -> int:
    match = _INSERTED_RE.search(message)
    if not match:
        return 0
    return int(match.group(1))


def main() -> int:
    load_dotenv_if_present()
    started = time.monotonic()

    try:
        dsn = env_required("SUPABASE_DB_URL")
        dune_key = env_required("DUNE_KEY")
        query_id = env_int("DUNE_QUERY_ID", DEFAULT_QUERY_ID)
        page_size = env_int("DUNE_PAGE_SIZE", DEFAULT_PAGE_SIZE)
        page_delay = env_float("DUNE_PAGE_DELAY_SECONDS", DEFAULT_PAGE_DELAY_SECONDS)
        chunk_size = env_int("UPSERT_CHUNK_SIZE", DEFAULT_UPSERT_CHUNK_SIZE)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    logger.info(
        "Starting token prices import (query_id=%s, page_size=%s, page_delay=%.1fs, chunk_size=%s)",
        query_id,
        page_size,
        page_delay,
        chunk_size,
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

    total_chunks = (len(rows) + chunk_size - 1) // chunk_size
    logger.info(
        "Fetched %s rows from Dune; upserting in %s chunk(s) of up to %s",
        len(rows),
        total_chunks,
        chunk_size,
    )

    db = Database(dsn)
    inserted_total = 0
    try:
        db.connect()
        for chunk_idx in range(total_chunks):
            start = chunk_idx * chunk_size
            chunk = rows[start : start + chunk_size]
            logger.info(
                "Upserting chunk %s/%s (%s rows)",
                chunk_idx + 1,
                total_chunks,
                len(chunk),
            )
            message = db.upsert_token_prices(chunk)
            inserted = _parse_inserted_count(message)
            inserted_total += inserted
            logger.info("Chunk %s/%s — %s", chunk_idx + 1, total_chunks, message)
    except Exception:
        logger.error("Upsert failed:\n%s", traceback.format_exc())
        return 1
    finally:
        db.close()

    elapsed = time.monotonic() - started
    logger.info(
        "Done in %.1fs — fetched %s rows, inserted %s new rows across %s chunk(s)",
        elapsed,
        len(rows),
        inserted_total,
        total_chunks,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
