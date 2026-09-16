#!/usr/bin/env python3
"""Print eligible wallet counts for both owner_wallet_monthly lanes."""

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

    db = Database(dsn)
    db.connect()
    try:
        eligible = {
            "monthly": db.count_eligible_wallets("monthly"),
            "origin": db.count_eligible_wallets("origin"),
        }
    finally:
        db.close()

    print(json.dumps(eligible))


if __name__ == "__main__":
    main()
