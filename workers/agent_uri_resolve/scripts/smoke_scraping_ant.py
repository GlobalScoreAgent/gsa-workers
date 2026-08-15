"""Smoke: ScrapingAnt connectivity for URI last-fallback.

Usage (PowerShell):
  $env:SCRAPING_ANT_KEY = '...'   # do not commit
  uv run python scripts/smoke_scraping_ant.py

Exit 0 = HTTP 2xx + body received. Does not print the API key.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlencode

import httpx

API_URL = "https://api.scrapingant.com/v2/general"
# Stable public page; browser=true matches production fallback.
TARGET_URL = "https://example.com/"


def main() -> int:
    key = (os.environ.get("SCRAPING_ANT_KEY") or "").strip()
    if not key:
        print("FAIL: SCRAPING_ANT_KEY is not set in the environment")
        return 2

    params = {
        "url": TARGET_URL,
        "browser": "true",
        "x-api-key": key,
    }
    url = f"{API_URL}?{urlencode(params)}"
    print(f"GET {API_URL}?url={TARGET_URL}&browser=true&x-api-key=***")

    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.get(url)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: request error: {type(exc).__name__}: {exc}")
        return 1

    credits = resp.headers.get("Ant-credits-cost") or resp.headers.get(
        "ant-credits-cost"
    )
    remaining = resp.headers.get("Ant-credits-remaining") or resp.headers.get(
        "ant-credits-remaining"
    )
    body_len = len(resp.content)
    body_preview = (resp.text or "").strip().replace("\n", " ")[:120]

    print(f"status={resp.status_code} body_bytes={body_len}")
    if credits is not None:
        print(f"Ant-credits-cost={credits}")
    if remaining is not None:
        print(f"Ant-credits-remaining={remaining}")
    print(f"body_preview={body_preview!r}")

    if not resp.is_success:
        print(f"FAIL: expected 2xx, got {resp.status_code}")
        return 1
    if body_len < 32:
        print("FAIL: body too short")
        return 1

    print("OK: ScrapingAnt reachable with this key (browser render)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
