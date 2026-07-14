from __future__ import annotations

import re
from typing import Any


def try_decode_deckard_rsc(html: str) -> dict[str, Any] | None:
    """Extract Next.js RSC payloads from Deckard pages (Edge parity)."""
    segments = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html)
    if not segments:
        return None

    parts: list[str] = []
    for s in segments:
        content = s
        content = content.replace("\\n", " ").replace('\\"', '"')
        content = content.replace("\\u003c", "<").replace("\\u003e", ">")
        parts.append(content)

    extracted = " ".join(parts)
    extracted = re.sub(r"<[^>]+>", " ", extracted)
    extracted = re.sub(r"\s+", " ", extracted).strip()
    if not extracted:
        return None
    return {
        "content_text": extracted,
        "content_type": "deckard_parsed_report",
    }
