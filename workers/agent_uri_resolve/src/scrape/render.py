from __future__ import annotations

import json
import re
from typing import Any

from result import ResolveResult
from scrape.deckard_rsc import try_decode_deckard_rsc


def html_to_content(html: str, content_type: str) -> dict[str, Any]:
    clean = re.sub(r"<[^>]+>", " ", html)
    clean = re.sub(r"\s+", " ", clean).strip()
    return {
        "content_text": clean[:15000],
        "content_type": content_type,
    }


def parse_rendered_body(uri: str, body: str, gateway_prefix: str) -> ResolveResult:
    text = body.strip()
    try:
        return ResolveResult(
            ok=True,
            document=json.loads(text),
            used_gateway=f"{gateway_prefix}-json",
        )
    except json.JSONDecodeError:
        pass

    if "deckard.network" in uri:
        decoded = try_decode_deckard_rsc(body)
        if decoded:
            return ResolveResult(
                ok=True,
                document=decoded,
                used_gateway=f"{gateway_prefix}-deckard-decoded",
            )
        return ResolveResult(
            ok=True,
            document=html_to_content(body, "web_page_clean"),
            used_gateway=f"{gateway_prefix}-standard-clean",
        )

    return ResolveResult(
        ok=True,
        document=html_to_content(body, "web_page_render"),
        used_gateway=f"{gateway_prefix}-render",
    )
