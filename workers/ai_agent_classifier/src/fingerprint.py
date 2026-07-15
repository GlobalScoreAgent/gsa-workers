"""Exact-match fingerprint of AI classifier prompt input fields."""

from __future__ import annotations

import hashlib
import json
from typing import Any

PROMPT_INPUT_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "skills",
    "tags",
    "capabilites",
    "services",
    "oasf_skills",
    "oasf_domains",
    "web",
)


def _normalize_for_hash(value: Any) -> Any:
    """Normalize empties so null / {} / [] / '' match within structure."""
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    if isinstance(value, dict):
        if not value:
            return None
        out: dict[str, Any] = {}
        for key, item in value.items():
            normalized = _normalize_for_hash(item)
            out[str(key)] = normalized
        return out if out else None
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        out_list = [_normalize_for_hash(item) for item in value]
        return out_list if out_list else None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    # Decimal / other scalars from drivers
    try:
        if hasattr(value, "as_integer_ratio"):
            return float(value)
    except Exception:
        pass
    return _normalize_for_hash(str(value))


def canonicalize_prompt_inputs(agent: dict[str, Any]) -> str:
    payload = {
        field: _normalize_for_hash(agent.get(field)) for field in PROMPT_INPUT_FIELDS
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def agent_input_hash(agent: dict[str, Any]) -> str:
    canonical = canonicalize_prompt_inputs(agent)
    return hashlib.md5(canonical.encode("utf-8")).hexdigest()
