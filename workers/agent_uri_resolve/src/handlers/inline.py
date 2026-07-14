from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import unquote

from result import ResolveResult


def try_hex(uri: str) -> ResolveResult | None:
    if not uri.startswith("0x"):
        return None
    try:
        hex_body = uri[2:]
        if len(hex_body) % 2:
            hex_body = "0" + hex_body
        raw = bytes.fromhex(hex_body)
        decoded = raw.decode("utf-8", errors="replace").strip()
        if decoded.startswith("{"):
            try:
                return ResolveResult(
                    ok=True,
                    document=json.loads(decoded),
                    used_gateway="hex-to-json",
                )
            except json.JSONDecodeError:
                return ResolveResult(
                    ok=True,
                    document={"text": decoded},
                    used_gateway="hex-to-text",
                )
        return ResolveResult(
            ok=True,
            document={"text": decoded},
            used_gateway="hex-to-text",
        )
    except Exception as exc:  # noqa: BLE001
        return ResolveResult(ok=False, error=f"Error decoding Hex: {exc}")


def try_raw_json(uri: str) -> ResolveResult | None:
    if not (uri.startswith("{") and uri.endswith("}")):
        return None
    try:
        return ResolveResult(
            ok=True,
            document=json.loads(uri),
            used_gateway="raw-json-inline",
        )
    except json.JSONDecodeError as exc:
        return ResolveResult(ok=False, error=f"Error parsing JSON: {exc}")


def try_data_uri(uri: str) -> ResolveResult | None:
    if not uri.startswith("data:application/json"):
        return None
    try:
        import gzip

        normalized = uri.replace(";base64 ", ";base64,")
        header, _, content = normalized.partition(",")
        if ";base64" in header:
            binary = base64_decode(content)
            if "enc=gzip" in header or "compression=gzip" in header:
                document: Any = json.loads(gzip.decompress(binary).decode("utf-8"))
                gateway = "base64-gzip-inline"
            else:
                document = json.loads(binary.decode("utf-8"))
                gateway = "base64-inline"
        else:
            document = json.loads(unquote(content))
            gateway = "data-inline-plain"
        return ResolveResult(ok=True, document=document, used_gateway=gateway)
    except Exception as exc:  # noqa: BLE001
        return ResolveResult(ok=False, error=f"Error processing Data URI: {exc}")


def base64_decode(content: str) -> bytes:
    cleaned = re.sub(r"[\s\t\n\r]+", "", content.strip())
    return base64.b64decode(cleaned)
