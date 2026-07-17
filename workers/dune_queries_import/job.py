#!/usr/bin/env python3
"""Import Dune reference queries into wallets.* tables."""

from __future__ import annotations

import logging
import os
import sys
import time
import traceback
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from db import DEFAULT_UPSERT_CHUNK_SIZE, Database
from dune import (
    DEFAULT_PAGE_DELAY_SECONDS,
    DEFAULT_PAGE_SIZE,
    HTTP_TIMEOUT_SECONDS,
    DuneError,
    fetch_latest_rows,
)
from tasks import TASKS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("dune_queries_import")

DEFAULT_TASK_DELAY_SECONDS = 3.0


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
        page_size = env_int("DUNE_PAGE_SIZE", DEFAULT_PAGE_SIZE)
        page_delay = env_float("DUNE_PAGE_DELAY_SECONDS", DEFAULT_PAGE_DELAY_SECONDS)
        task_delay = env_float("DUNE_TASK_DELAY_SECONDS", DEFAULT_TASK_DELAY_SECONDS)
        chunk_size = env_int("UPSERT_CHUNK_SIZE", DEFAULT_UPSERT_CHUNK_SIZE)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    logger.info(
        "Starting Dune queries import (%s tasks, page_size=%s, page_delay=%.1fs, "
        "task_delay=%.1fs, chunk_size=%s)",
        len(TASKS),
        page_size,
        page_delay,
        task_delay,
        chunk_size,
    )

    db = Database(dsn)
    failures: list[str] = []

    try:
        db.connect()
        with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as http:
            for index, task in enumerate(TASKS):
                if index > 0 and task_delay > 0:
                    logger.info(
                        "Waiting %.1fs before next task (rate-limit pacing)",
                        task_delay,
                    )
                    time.sleep(task_delay)

                logger.info(
                    "=== Task %s/%s: %s (query_id=%s) ===",
                    index + 1,
                    len(TASKS),
                    task.name,
                    task.query_id,
                )
                try:
                    rows = fetch_latest_rows(
                        dune_key,
                        task.query_id,
                        page_size=page_size,
                        page_delay_seconds=page_delay,
                        client=http,
                    )
                    if not rows:
                        raise DuneError(
                            f"Dune query {task.query_id} returned 0 rows; "
                            "refusing to upsert empty payload"
                        )
                    logger.info(
                        "Fetched %s rows for %s; upserting in chunks of %s",
                        len(rows),
                        task.name,
                        chunk_size,
                    )
                    message = db.upsert_rows_chunked(
                        task_name=task.name,
                        rpc_sql=task.rpc_sql,
                        rows=rows,
                        chunk_size=chunk_size,
                    )
                    logger.info("Task %s OK — %s", task.name, message)
                except Exception as exc:
                    failures.append(task.name)
                    logger.error(
                        "Task %s failed: %s\n%s",
                        task.name,
                        exc,
                        traceback.format_exc(),
                    )
    finally:
        db.close()

    elapsed = time.monotonic() - started
    if failures:
        logger.error(
            "Finished in %.1fs with failures: %s",
            elapsed,
            ", ".join(failures),
        )
        return 1

    logger.info("All %s tasks succeeded in %.1fs", len(TASKS), elapsed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
