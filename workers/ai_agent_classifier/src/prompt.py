"""User prompt builder and response validation for agent AI classification.

System prompt is loaded from llm.process.system_prompt (not hardcoded here).
"""

from __future__ import annotations

import json
from typing import Any


def _field_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        return str(value)


def build_user_prompt(*, categories: list[str], agent: dict[str, Any]) -> str:
    categories_text = ", ".join(categories)
    return (
        f"Available categories: {categories_text}\n"
        "Analyze this agent and return the classification in the required JSON format.\n\n"
        "Agent Information:\n"
        f"Name: {_field_to_text(agent.get('name'))}\n"
        f"Description: {_field_to_text(agent.get('description'))}\n"
        f"Skills: {_field_to_text(agent.get('skills'))}\n"
        f"Tags: {_field_to_text(agent.get('tags'))}\n"
        f"Capabilities: {_field_to_text(agent.get('capabilites'))}\n"
        f"Services: {_field_to_text(agent.get('services'))}\n"
        f"Osaf_Skills: {_field_to_text(agent.get('oasf_skills'))}\n"
        f"Osaf_Domains: {_field_to_text(agent.get('oasf_domains'))}\n"
        f"Web: {_field_to_text(agent.get('web'))}\n"
    )


def parse_classification_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("classification root must be a JSON object")
    return data


def validate_classification(
    data: dict[str, Any],
    *,
    allowed_categories: set[str],
) -> dict[str, Any]:
    primary = data.get("primary_category")
    if not isinstance(primary, str) or not primary.strip():
        raise ValueError("primary_category is required")
    primary = primary.strip()
    if primary not in allowed_categories:
        raise ValueError(f"primary_category not in allowed list: {primary!r}")

    secondary_raw = data.get("secondary_categories") or []
    if not isinstance(secondary_raw, list):
        raise ValueError("secondary_categories must be a list")
    secondary: list[str] = []
    for item in secondary_raw:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if name and name in allowed_categories and name not in secondary:
            secondary.append(name)

    confidence = data.get("confidence")
    conf_value: float | None
    if confidence is None:
        conf_value = None
    else:
        conf_value = float(confidence)

    reasoning = data.get("reasoning")
    purpose = data.get("agent_purpose")
    if purpose is None or not str(purpose).strip():
        raise ValueError("agent_purpose is required")

    return {
        "primary_category": primary,
        "secondary_categories": secondary,
        "confidence": conf_value,
        "reasoning": None if reasoning is None else str(reasoning).strip(),
        "agent_purpose": str(purpose).strip(),
    }
