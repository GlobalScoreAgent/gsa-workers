#!/usr/bin/env python3
"""Smoke-test origin queries for representative wallet types."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from origin import query_all_chains_origin

TEST_ADDRESSES = [
    ("eoa", "0x3195c3f94154364e897711e501e104f40d8e23fb"),
    ("contract", "0xdac17f958d2ee523a2206206994597c13d831ec7"),
    ("no_activity", "0x1111111111111111111111111111111111111111"),
]


def summarize(chain_results: list[dict]) -> dict:
    return {
        item["key"]: {
            "status": item.get("status"),
            "block": item.get("block"),
            "type": item.get("type"),
        }
        for item in chain_results
    }


async def main() -> None:
    report: dict = {}
    for label, address in TEST_ADDRESSES:
        results = await query_all_chains_origin(address, {}, None)
        report[label] = {"address": address, "chains": summarize(results)}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
