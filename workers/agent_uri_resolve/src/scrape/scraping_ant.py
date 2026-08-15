from __future__ import annotations

import logging
from urllib.parse import urlencode

import httpx

from result import ResolveResult
from scrape.render import parse_rendered_body

logger = logging.getLogger("agent_uri_resolve.scraping_ant")

API_URL = "https://api.scrapingant.com/v2/general"


async def fetch_scraping_ant(
    uri: str,
    api_key: str,
    client: httpx.AsyncClient,
) -> ResolveResult:
    """Last-resort HTTP fallback via ScrapingAnt (browser render)."""
    params = {
        "url": uri,
        "browser": "true",
        "x-api-key": api_key,
    }
    try:
        # Build URL manually so urlencode handles the target URI once.
        resp = await client.get(
            f"{API_URL}?{urlencode(params)}",
            timeout=60.0,
        )
        if not resp.is_success:
            return ResolveResult(
                ok=False,
                error=f"scraping_ant_http_{resp.status_code}",
            )
        return parse_rendered_body(uri, resp.text, "scraping-ant")
    except Exception as exc:  # noqa: BLE001
        logger.warning("ScrapingAnt failed: %s", exc)
        return ResolveResult(ok=False, error=f"scraping_ant_error:{exc}")
