from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from result import ResolveResult

logger = logging.getLogger("agent_uri_resolve.ipfs")

CID_RE = re.compile(r"([a-zA-Z0-9]{46,64})")

FREE_GATEWAYS = [
    ("ipfs-io", "https://ipfs.io/ipfs/"),
    ("pinata-gateway-public", "https://gateway.pinata.cloud/ipfs/"),
    ("cloudflare-ipfs", "https://cloudflare-ipfs.com/ipfs/"),
    ("dweb-link", "https://dweb.link/ipfs/"),
]

PINATA_DEDICATED_BASE = "https://indigo-urban-flyingfish-439.mypinata.cloud/ipfs/"
TIMEOUT_PUBLIC = 12.0
TIMEOUT_DEDICATED = 20.0
MAX_RETRIES = 2


def looks_like_ipfs(uri: str) -> bool:
    if not CID_RE.search(uri):
        return False
    return (not uri.startswith("http")) or ("/ipfs/" in uri)


def extract_cid(uri: str) -> str | None:
    match = CID_RE.search(uri)
    return match.group(1) if match else None


async def fetch_ipfs(
    uri: str,
    client: httpx.AsyncClient,
    pinata_token: str = "",
) -> ResolveResult:
    cid = extract_cid(uri)
    if not cid:
        return ResolveResult(ok=False, error="No CID found in URI")

    gateways: list[tuple[str, str, dict[str, str], float]] = [
        (name, f"{base}{cid}", {}, TIMEOUT_PUBLIC) for name, base in FREE_GATEWAYS
    ]
    if pinata_token.strip():
        gateways.append(
            (
                "pinata-dedicated",
                f"{PINATA_DEDICATED_BASE}{cid}",
                {"x-pinata-gateway-token": pinata_token.strip()},
                TIMEOUT_DEDICATED,
            )
        )

    last_error = "ipfs_all_gateways_failed"
    for name, url, extra_headers, timeout in gateways:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                headers = {
                    "Accept": "application/json",
                    "User-Agent": "gsa-agent-uri-resolve",
                    **extra_headers,
                }
                resp = await client.get(url, headers=headers, timeout=timeout)
                if not resp.is_success:
                    last_error = f"ipfs_{name}_http_{resp.status_code}"
                    continue
                text = resp.text.strip()
                if not (text.startswith("{") or text.startswith("[")):
                    last_error = f"ipfs_{name}_not_json"
                    continue
                document: Any = json.loads(text)
                return ResolveResult(ok=True, document=document, used_gateway=name)
            except Exception as exc:  # noqa: BLE001
                last_error = f"ipfs_{name}_error:{exc}"
                logger.debug("IPFS %s attempt %s failed: %s", name, attempt, exc)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(0.4 * attempt)
    return ResolveResult(ok=False, error=last_error, used_gateway=None)
