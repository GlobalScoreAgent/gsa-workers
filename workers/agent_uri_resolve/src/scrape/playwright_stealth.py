from __future__ import annotations

import logging

from result import ResolveResult
from scrape.render import parse_rendered_body

logger = logging.getLogger("agent_uri_resolve.playwright")


async def fetch_with_playwright(uri: str, wait_ms: int = 4000) -> ResolveResult:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return ResolveResult(ok=False, error="playwright_not_installed")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                )
                page = await context.new_page()
                try:
                    from playwright_stealth import Stealth

                    await Stealth().apply_stealth_async(page)
                except Exception:  # noqa: BLE001
                    logger.debug("playwright-stealth unavailable; continuing without it")

                await page.goto(uri, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(wait_ms)
                body = await page.content()
                return parse_rendered_body(uri, body, "playwright")
            finally:
                await browser.close()
    except Exception as exc:  # noqa: BLE001
        logger.info("Playwright failed for %s: %s", uri[:100], exc)
        return ResolveResult(ok=False, error=f"playwright_error:{exc}")
