from __future__ import annotations

import json
import logging
import re

import httpx

from result import ResolveResult
from scrape.deckard_http import fetch_deckard_clean
from scrape.playwright_stealth import fetch_with_playwright
from scrape.scrape_do import fetch_scrape_do

logger = logging.getLogger("agent_uri_resolve.http")

TIMEOUT_PUBLIC = 12.0


def looks_like_http(uri: str) -> bool:
    return bool(re.match(r"^https?://", uri, flags=re.I))


async def fetch_http(
    uri: str,
    client: httpx.AsyncClient,
    scrape_do_token: str = "",
) -> ResolveResult:
    try:
        resp = await client.get(
            uri,
            headers={
                "Accept": "application/json",
                "User-Agent": "gsa-agent-uri-resolve",
            },
            timeout=TIMEOUT_PUBLIC,
            follow_redirects=True,
        )
        if resp.is_success:
            text = resp.text
            try:
                return ResolveResult(
                    ok=True,
                    document=json.loads(text),
                    used_gateway="direct-fetch-json",
                )
            except json.JSONDecodeError:
                logger.info("Direct fetch not JSON; cascading scrapers for %s", uri[:120])
    except Exception as exc:  # noqa: BLE001
        logger.info("Direct fetch failed (%s); cascading scrapers", exc)

    pw = await fetch_with_playwright(uri)
    if pw.ok and pw.document is not None:
        return pw

    deck = await fetch_deckard_clean(uri, client)
    if deck.ok and deck.document is not None:
        return deck

    if scrape_do_token.strip():
        return await fetch_scrape_do(uri, scrape_do_token.strip(), client)

    return ResolveResult(
        ok=False,
        error=pw.error or deck.error or "http_all_fallbacks_failed",
        used_gateway=None,
    )
