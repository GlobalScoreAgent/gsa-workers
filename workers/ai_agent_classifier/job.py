#!/usr/bin/env python3
"""Classify web_dashboard.agents with LLM categories (process_code=agent-classifier)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
import traceback
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Literal

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from db import CLAIM_RETRY_BASE_SECONDS, Database
from llm_client import chat_completion
from prompt import (
    build_user_prompt,
    parse_classification_json,
    validate_classification,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("ai_agent_classifier")

Outcome = Literal["ok", "error", "capacity"]
LLM_429_MAX_ATTEMPTS = 3
_RETRY_AFTER_MS_RE = re.compile(r"try again in\s+(\d+(?:\.\d+)?)\s*ms", re.IGNORECASE)
_RETRY_AFTER_S_RE = re.compile(r"try again in\s+(\d+(?:\.\d+)?)\s*s(?:ec(?:onds?)?)?", re.IGNORECASE)


def env_int(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        value = default
    else:
        value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def model_has_capacity(model: dict[str, Any]) -> bool:
    if not bool(model.get("has_limits")):
        return True
    used = int(model.get("request_total_today") or 0)
    limit = int(model.get("request_per_day") or 0)
    return used < limit


def pick_model(models: list[dict[str, Any]]) -> dict[str, Any] | None:
    for model in models:
        if model_has_capacity(model):
            return model
    return None


def resolve_api_key(secret_name: str) -> str:
    key = os.environ.get(secret_name)
    if key is None or not str(key).strip():
        raise RuntimeError(f"Missing API key env for provider secret={secret_name!r}")
    return str(key).strip()


class RateLimiter:
    """Hardcap: at most request_per_minute calls per model in any rolling 60s window."""

    WINDOW_SECONDS = 60.0

    def __init__(self) -> None:
        self._windows: dict[int, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def wait(self, model_id: int, request_per_minute: int) -> None:
        rpm = max(1, int(request_per_minute or 1))
        while True:
            async with self._lock:
                now = time.monotonic()
                window = self._windows[model_id]
                while window and (now - window[0]) >= self.WINDOW_SECONDS:
                    window.popleft()
                if len(window) < rpm:
                    window.append(now)
                    return
                sleep_for = self.WINDOW_SECONDS - (now - window[0]) + 0.01
                logger.info(
                    "RPM hardcap model_id=%s rpm=%s in_window=%s; sleeping %.2fs",
                    model_id,
                    rpm,
                    len(window),
                    sleep_for,
                )
            await asyncio.sleep(max(sleep_for, 0.05))


def _is_http_429(exc: BaseException) -> bool:
    return "HTTP 429" in str(exc)


def _retry_after_seconds(exc: BaseException, attempt: int) -> float:
    text = str(exc)
    m_ms = _RETRY_AFTER_MS_RE.search(text)
    if m_ms:
        return max(float(m_ms.group(1)) / 1000.0, 0.05)
    m_s = _RETRY_AFTER_S_RE.search(text)
    if m_s:
        return max(float(m_s.group(1)), 0.05)
    return float(2 ** (attempt - 1))


async def call_llm_with_retries(
    http_client: httpx.AsyncClient,
    *,
    model: dict[str, Any],
    api_key: str,
    system_prompt: str,
    user_prompt: str,
    rate_limiter: RateLimiter,
    bump_model_total,
) -> str:
    model_id = int(model["model_id"])
    rpm = int(model["request_per_minute"] or 1)
    last_exc: Exception | None = None
    for attempt in range(1, LLM_429_MAX_ATTEMPTS + 1):
        await rate_limiter.wait(model_id, rpm)
        try:
            raw = await chat_completion(
                http_client,
                base_url=str(model["base_url"]),
                api_key=api_key,
                model_slug=str(model["model_slug"]),
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=(
                    None
                    if model.get("temperature") is None
                    else float(model["temperature"])
                ),
                max_completion_tokens=(
                    None
                    if model.get("max_completion_tokens") is None
                    else int(model["max_completion_tokens"])
                ),
                response_format=(
                    None
                    if model.get("response_format") is None
                    else str(model["response_format"])
                ),
            )
            await bump_model_total(model_id)
            return raw
        except Exception as exc:
            await bump_model_total(model_id)
            last_exc = exc
            if _is_http_429(exc) and attempt < LLM_429_MAX_ATTEMPTS:
                delay = _retry_after_seconds(exc, attempt)
                logger.warning(
                    "LLM HTTP 429 model_id=%s attempt=%s/%s; sleeping %.2fs",
                    model_id,
                    attempt,
                    LLM_429_MAX_ATTEMPTS,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            raise
    assert last_exc is not None
    raise last_exc


async def run_job() -> int:
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        logger.error("SUPABASE_DB_URL is required")
        return 1

    claim_batch_size = env_int("CLAIM_BATCH_SIZE", default=20, minimum=1, maximum=200)
    concurrency = env_int("CONCURRENCY", default=1, minimum=1, maximum=5)
    max_runtime_seconds = env_int("MAX_RUNTIME_SECONDS", default=19800, minimum=60)

    db = Database(dsn)
    db.connect()

    try:
        categories = db.load_active_categories()
        system_prompt = db.load_system_prompt()
    except Exception as exc:
        logger.error("Failed to load categories/system_prompt: %s", exc)
        db.close()
        return 1

    if not categories:
        logger.error("No active categories in web_dashboard.agent_ai_categories")
        db.close()
        return 1

    if not system_prompt:
        logger.error(
            "Missing llm.process.system_prompt for process_code=agent-classifier"
        )
        db.close()
        return 1

    allowed = set(categories)
    logger.info(
        "Started categories=%s system_prompt_chars=%s claim_batch_size=%s "
        "concurrency=%s max_runtime=%ss",
        len(categories),
        len(system_prompt),
        claim_batch_size,
        concurrency,
        max_runtime_seconds,
    )

    start = time.monotonic()
    processed = 0
    completed = 0
    errors = 0
    sem = asyncio.Semaphore(concurrency)
    db_lock = asyncio.Lock()
    rate_limiter = RateLimiter()
    models_cache: list[dict[str, Any]] = []
    http_limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)

    async def refresh_models() -> list[dict[str, Any]]:
        nonlocal models_cache
        async with db_lock:
            models_cache = db.load_process_models()
        return models_cache

    async def bump_model_total(model_id: int) -> None:
        async with db_lock:
            total = db.increment_model_request(model_id)
            for i, m in enumerate(models_cache):
                if int(m["model_id"]) == model_id:
                    models_cache[i] = {**m, "request_total_today": total}
                    break

    try:
        await refresh_models()
        if not models_cache:
            logger.error(
                "No active models for process_code=agent-classifier. "
                "Check llm.procees_llm_providers + llm.models."
            )
            return 1

        for model in models_cache:
            resolve_api_key(str(model["provider_secret"]))

        async with httpx.AsyncClient(timeout=60.0, limits=http_limits) as http_client:
            while True:
                elapsed = time.monotonic() - start
                if elapsed >= max_runtime_seconds:
                    logger.info(
                        "Time budget reached (%.0fs). Processed=%s completed=%s errors=%s",
                        elapsed,
                        processed,
                        completed,
                        errors,
                    )
                    break

                await refresh_models()
                if pick_model(models_cache) is None:
                    logger.info(
                        "All models at daily request_per_day limit. Exiting (exit 0)."
                    )
                    break

                async with db_lock:
                    try:
                        rows = db.claim_agents(limit=claim_batch_size)
                    except Exception as exc:
                        logger.error("Claim failed; will retry next loop: %s", exc)
                        await asyncio.sleep(CLAIM_RETRY_BASE_SECONDS)
                        continue

                if not rows:
                    if processed == 0:
                        logger.info("No pending agents. Exiting.")
                    else:
                        logger.info("No more pending agents in this run.")
                    break

                logger.info(
                    "Claimed batch size=%s first_id=%s last_id=%s",
                    len(rows),
                    rows[0]["id"],
                    rows[-1]["id"],
                )

                async def handle_agent(agent: dict[str, Any]) -> Outcome:
                    agent_id = int(agent["id"])
                    async with sem:
                        model: dict[str, Any] | None = None
                        try:
                            async with db_lock:
                                current = db.load_process_models()
                                model = pick_model(current)

                            if model is None:
                                logger.info(
                                    "No model capacity for agent_id=%s; leaving pending",
                                    agent_id,
                                )
                                return "capacity"

                            api_key = resolve_api_key(str(model["provider_secret"]))
                            user_prompt = build_user_prompt(
                                categories=categories,
                                agent=agent,
                            )
                            raw = await call_llm_with_retries(
                                http_client,
                                model=model,
                                api_key=api_key,
                                system_prompt=system_prompt,
                                user_prompt=user_prompt,
                                rate_limiter=rate_limiter,
                                bump_model_total=bump_model_total,
                            )

                            parsed = parse_classification_json(raw)
                            validated = validate_classification(
                                parsed,
                                allowed_categories=allowed,
                            )
                            async with db_lock:
                                db.mark_success(
                                    agent_id=agent_id,
                                    llm_model_id=int(model["model_id"]),
                                    primary_category=validated["primary_category"],
                                    secondary_categories=validated[
                                        "secondary_categories"
                                    ],
                                    confidence=validated["confidence"],
                                    reasoning=validated["reasoning"],
                                    agent_purpose=validated["agent_purpose"],
                                )
                            logger.info(
                                "Done agent_id=%s model_id=%s primary=%s",
                                agent_id,
                                model["model_id"],
                                validated["primary_category"],
                            )
                            return "ok"
                        except Exception as exc:
                            err_text = f"{exc.__class__.__name__}: {exc}"
                            logger.warning("Agent id=%s failed: %s", agent_id, err_text)
                            try:
                                async with db_lock:
                                    db.mark_error(
                                        agent_id=agent_id,
                                        error_message=err_text,
                                        llm_model_id=(
                                            None
                                            if model is None
                                            else int(model["model_id"])
                                        ),
                                    )
                            except Exception as mark_exc:
                                logger.error(
                                    "mark_error failed agent_id=%s: %s",
                                    agent_id,
                                    mark_exc,
                                )
                            return "error"

                outcomes = await asyncio.gather(*(handle_agent(row) for row in rows))
                capacity_hit = False
                for outcome in outcomes:
                    if outcome == "ok":
                        processed += 1
                        completed += 1
                    elif outcome == "error":
                        processed += 1
                        errors += 1
                    else:
                        capacity_hit = True

                if capacity_hit:
                    logger.info("Daily model capacity exhausted mid-batch. Exiting.")
                    break

                await refresh_models()
                if pick_model(models_cache) is None:
                    logger.info("Daily model capacity exhausted after batch. Exiting.")
                    break

    except Exception:
        logger.error("Critical job failure:\n%s", traceback.format_exc())
        return 1
    finally:
        db.close()

    logger.info(
        "Finished processed=%s completed=%s errors=%s elapsed=%.0fs",
        processed,
        completed,
        errors,
        time.monotonic() - start,
    )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run_job()))


if __name__ == "__main__":
    main()
