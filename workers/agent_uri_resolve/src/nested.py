"""Nested text pointers + DID URI extraction / normalization."""

from __future__ import annotations

import re
from typing import Any


def extract_nested_uri(document: Any) -> str | None:
    if not isinstance(document, dict):
        return None
    nested = document.get("text")
    if isinstance(nested, str) and _is_pointer(nested):
        return nested.strip()
    data = document.get("Data")
    if isinstance(data, dict):
        nested = data.get("text")
        if isinstance(nested, str) and _is_pointer(nested):
            return nested.strip()
    return None


def extract_did_uri(document: Any) -> str | None:
    if not isinstance(document, dict):
        return None
    candidates = [
        document.get("didDocumentUrl"),
        document.get("didDocumentCid"),
    ]
    data = document.get("Data")
    if isinstance(data, dict):
        candidates.extend(
            [
                data.get("didDocumentUrl"),
                data.get("didDocumentCid"),
            ]
        )
    for raw in candidates:
        if isinstance(raw, str) and raw.strip():
            return normalize_did_uri(raw.strip())
    return None


def normalize_did_uri(uri: str) -> str:
    if re.search(r"/ipfs/[a-zA-Z0-9]{46,64}", uri):
        cid = uri.rsplit("/", 1)[-1]
        if len(cid) >= 46:
            return f"https://ipfs.io/ipfs/{cid}"
    if not uri.startswith("http") and len(uri) >= 46 and re.fullmatch(
        r"[a-zA-Z0-9]{46,64}", uri
    ):
        return f"https://ipfs.io/ipfs/{uri}"
    return uri


def inject_did_json(document: Any, did_json: Any) -> Any:
    if not isinstance(document, dict):
        return document
    out = dict(document)
    if "Data" in out and isinstance(out["Data"], dict):
        data = dict(out["Data"])
        data["didDocumentJson"] = did_json
        out["Data"] = data
    else:
        out["didDocumentJson"] = did_json
    return out


def replace_with_nested(document: Any, nested_doc: Any) -> Any:
    return nested_doc


def _is_pointer(value: str) -> bool:
    v = value.strip()
    return bool(
        re.match(r"^https?://", v, flags=re.I)
        or v.startswith("ipfs://")
        or v.startswith("data:application/json")
    )
