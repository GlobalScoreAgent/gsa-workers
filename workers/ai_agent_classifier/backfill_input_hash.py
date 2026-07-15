#!/usr/bin/env python3
"""Backfill web_dashboard.agents.ai_category_input_hash for classified donors.

Uses the same fingerprint helper as the classifier runtime.

  uv run python backfill_input_hash.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from db import Database
from fingerprint import agent_input_hash

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_input_hash")

BATCH_SIZE = int(os.environ.get("BACKFILL_BATCH_SIZE", "1000"))


def main() -> int:
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        logger.error("SUPABASE_DB_URL is required")
        return 1

    db = Database(dsn)
    db.connect()
    updated = 0
    try:
        while True:
            rows = db.fetch_agents_missing_input_hash(limit=BATCH_SIZE)
            if not rows:
                break
            for row in rows:
                h = agent_input_hash(row)
                db.set_ai_category_input_hash(agent_id=int(row["id"]), input_hash=h)
                updated += 1
            logger.info("Updated batch size=%s total=%s", len(rows), updated)
        logger.info("Backfill complete updated=%s", updated)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
