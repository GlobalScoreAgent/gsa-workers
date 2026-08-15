#!/usr/bin/env python3
"""Import ERC-8257 tools from agenttoolindex.xyz into erc_8257.tools."""

from __future__ import annotations

import logging
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from agenttoolindex import (
    DEFAULT_BASE_URL,
    HTTP_TIMEOUT_SECONDS,
    fetch_full_dump,
    fetch_stats,
)
from db import DEFAULT_UPSERT_CHUNK_SIZE, Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("erc8257_tools_import")


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


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


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


def parse_synced_at(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # API may emit >6 fractional digits; Postgres timestamptz is microseconds.
    if "." in text:
        head, frac_and_tz = text.split(".", 1)
        digits = ""
        tz = ""
        for i, ch in enumerate(frac_and_tz):
            if ch.isdigit():
                digits += ch
            else:
                tz = frac_and_tz[i:]
                break
        text = f"{head}.{digits[:6].ljust(6, '0')}{tz}"
    return datetime.fromisoformat(text)


def main() -> int:
    load_dotenv_if_present()
    started = time.monotonic()

    try:
        dsn = env_required("SUPABASE_DB_URL")
        base_url = os.environ.get("AGENTTOOLINDEX_BASE_URL", DEFAULT_BASE_URL).strip()
        chunk_size = env_int("UPSERT_CHUNK_SIZE", DEFAULT_UPSERT_CHUNK_SIZE)
        force = env_bool("FORCE_FULL_SYNC", False)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    logger.info(
        "Starting ERC-8257 tools import (base_url=%s, chunk_size=%s, force=%s)",
        base_url,
        chunk_size,
        force,
    )

    db = Database(dsn)
    source_synced_at: datetime | None = None
    tool_count: int | None = None

    try:
        db.connect()
        with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as http:
            stats = fetch_stats(http, base_url=base_url)
            source_synced_at = parse_synced_at(stats.get("synced_at"))
            raw_count = stats.get("tool_count")
            tool_count = int(raw_count) if raw_count is not None else None

            logger.info(
                "Stats synced_at=%s tool_count=%s chains=%s stats=%s",
                source_synced_at,
                tool_count,
                stats.get("chains"),
                stats.get("stats"),
            )

            sync_state = db.get_sync_state()
            stored = None if sync_state is None else sync_state.get("source_synced_at")
            if (
                not force
                and source_synced_at is not None
                and stored is not None
                and stored == source_synced_at
            ):
                db.update_sync_state(
                    source_synced_at=source_synced_at,
                    last_tool_count=tool_count,
                    last_upserted=0,
                    last_status="skipped_unchanged",
                    last_error=None,
                )
                logger.info(
                    "skipped_unchanged source_synced_at=%s (no dump)",
                    source_synced_at,
                )
                return 0

            rows = fetch_full_dump(http, base_url=base_url)
            message = db.upsert_rows_chunked(rows=rows, chunk_size=chunk_size)
            db.update_sync_state(
                source_synced_at=source_synced_at,
                last_tool_count=tool_count if tool_count is not None else len(rows),
                last_upserted=len(rows),
                last_status="ok",
                last_error=None,
            )
            logger.info("Sync OK — %s", message)
    except Exception as exc:
        logger.error("Sync failed: %s\n%s", exc, traceback.format_exc())
        try:
            db.update_sync_state(
                source_synced_at=None,
                last_tool_count=tool_count,
                last_upserted=None,
                last_status="error",
                last_error=str(exc)[:2000],
            )
        except Exception:
            logger.exception("Failed to persist sync_state error")
        return 1
    finally:
        db.close()

    elapsed = time.monotonic() - started
    logger.info("Finished in %.1fs", elapsed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
