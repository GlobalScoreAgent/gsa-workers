#!/usr/bin/env python3
"""Build GHA matrix JSON for token activity probe (budget = 7 cells).

Emits shards from chains.token_activity_runner_count (BSC/Base/ETH) plus a
fixed `_rest` flex cell. Fails if include length != 7.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from db import Database
from networks import NETWORKS

EXPECTED_MATRIX_CELLS = 7
REST_CELL = {"chain": "_rest", "shard": 0, "shards": 1}


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

    cells.append(dict(REST_CELL))
    if len(cells) != EXPECTED_MATRIX_CELLS:
        print(
            f"Matrix must have exactly {EXPECTED_MATRIX_CELLS} cells, got {len(cells)}: "
            f"{cells}",
            file=sys.stderr,
        )
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
