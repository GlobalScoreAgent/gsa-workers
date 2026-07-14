"""Deckard / generic HTML cleaner (from deckard_scraper.zip) — returns dict, no filesystem."""

from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup

from result import ResolveResult

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


async def fetch_deckard_clean(
    uri: str,
    client: httpx.AsyncClient,
    timeout: float = 10.0,
) -> ResolveResult:
    try:
        resp = await client.get(
            uri,
            headers=DEFAULT_HEADERS,
            timeout=timeout,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        return ResolveResult(ok=False, error=f"deckard_http_error:{exc}")

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in ("script", "style", "noscript", "meta", "head"):
            for element in soup.find_all(tag):
                element.decompose()
        body = soup.find("body")
        raw_text = body.get_text(separator=" ") if body else soup.get_text(separator=" ")
        clean_text = re.sub(r"\s+", " ", raw_text).strip()
        if not clean_text:
            return ResolveResult(ok=False, error="deckard_http_empty_body")
        return ResolveResult(
            ok=True,
            document={"source": "direct", "content_text": clean_text},
            used_gateway="deckard-http-clean",
        )
    except Exception as exc:  # noqa: BLE001
        return ResolveResult(ok=False, error=f"deckard_http_parse:{exc}")
