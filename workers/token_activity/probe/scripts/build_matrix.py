#!/usr/bin/env python3
"""Build GHA matrix JSON from erc_8004.chains.token_activity_runner_count.

Prints JSON object to stdout:
  {"include":[{"chain":"ethereum","shard":0,"shards":2}, ...]}

Chains with runner_count < 1 are omitted (capacity pause).
Writes GitHub Actions output `matrix` when GITHUB_OUTPUT is set (heredoc-safe).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from db import Database
from networks import NETWORKS


def main() -> int:
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        print("SUPABASE_DB_URL is required", file=sys.stderr)
        return 1

    evm_ids = [int(n["evm_chain_id"]) for n in NETWORKS.values()]
    db = Database(dsn)
    db.connect()
    try:
        cells = db.list_matrix_cells(evm_ids)
    finally:
        db.close()

    if not cells:
        print("No active chains for matrix", file=sys.stderr)
        return 1

    matrix_obj = {"include": cells}
    payload = json.dumps(matrix_obj, separators=(",", ":"))
    print(payload)

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write("matrix<<EOF\n")
            fh.write(payload + "\n")
            fh.write("EOF\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
