"""OpenAI-compatible chat completions client (Groq / Cerebras / Gemini compat)."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger("ai_agent_classifier")


def _normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def _parse_response_format(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("provider response_format must be a JSON object")
    return parsed


async def chat_completion(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str,
    model_slug: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float | None,
    max_completion_tokens: int | None,
    response_format: str | None,
    timeout_seconds: float = 60.0,
) -> str:
    url = f"{_normalize_base_url(base_url)}/chat/completions"
    body: dict[str, Any] = {
        "model": model_slug,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if temperature is not None:
        body["temperature"] = float(temperature)
    if max_completion_tokens is not None:
        body["max_tokens"] = int(max_completion_tokens)
    fmt = _parse_response_format(response_format)
    if fmt is not None:
        body["response_format"] = fmt

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    resp = await client.post(url, headers=headers, json=body, timeout=timeout_seconds)
    if resp.status_code >= 400:
        snippet = (resp.text or "")[:500]
        raise RuntimeError(f"LLM HTTP {resp.status_code}: {snippet}")

    data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected LLM response shape: {data!r}") from exc
    if content is None or not str(content).strip():
        raise RuntimeError("LLM returned empty content")
    return str(content)
