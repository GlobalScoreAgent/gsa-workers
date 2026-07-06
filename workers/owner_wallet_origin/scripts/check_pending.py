#!/usr/bin/env python3
"""Print eligible wallet count for owner_wallet_origin queue checks."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from db import Database


def main() -> None:
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        print(json.dumps({"error": "SUPABASE_DB_URL is required"}))
        raise SystemExit(1)

    stale_seconds = int(os.environ.get("CLAIM_STALE_SECONDS", "7200"))
    db = Database(dsn)
    db.connect()
    try:
        eligible = db.count_eligible_wallets(stale_seconds)
    finally:
        db.close()

    print(json.dumps({"eligible": eligible}))


if __name__ == "__main__":
    main()
