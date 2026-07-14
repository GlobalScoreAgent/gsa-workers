from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from result import ResolveResult
from scrape.render import parse_rendered_body

logger = logging.getLogger("agent_uri_resolve.scrape_do")


async def fetch_scrape_do(
    uri: str,
    token: str,
    client: httpx.AsyncClient,
) -> ResolveResult:
    proxy = (
        f"https://api.scrape.do?token={token}"
        f"&url={quote(uri, safe='')}&render=true&wait=4000"
    )
    try:
        resp = await client.get(proxy, timeout=60.0)
        if not resp.is_success:
            return ResolveResult(
                ok=False,
                error=f"scrape_do_http_{resp.status_code}",
            )
        return parse_rendered_body(uri, resp.text, "scrape-do")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Scrape.do failed: %s", exc)
        return ResolveResult(ok=False, error=f"scrape_do_error:{exc}")
