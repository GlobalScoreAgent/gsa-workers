#!/usr/bin/env python3
"""Resolve agent + feedback URIs into uri_documents and agent_manifest."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from db import (
    CLAIM_RETRY_BASE_SECONDS,
    Database,
    feedback_uri_and_source,
    processed_type_for_uri,
)
from nested import extract_did_uri, normalize_did_uri
from resolve import UriResolver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("agent_uri_resolve")


def load_dotenv_if_present() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def env_int(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    raw = os.environ.get(name)
    value = default if raw is None or raw.strip() == "" else int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def env_str(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def truncate_uri_for_error(uri: str | None, limit: int = 500) -> str:
    text = (uri or "").strip() or "unknown"
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


async def process_agent(
    db: Database,
    resolver: UriResolver,
    row: dict[str, Any],
    sem: asyncio.Semaphore,
) -> None:
    async with sem:
        agent_id = int(row["id"])
        uri = (row.get("agent_uri_raw") or "").strip()
        try:
            result = await resolver.resolve(uri)
            ok = result.ok and result.document is not None
            db.upsert_manifest(
                {
                    "agent_id": agent_id,
                    "provider": "erc-8004",
                    "uri_document_id": result.uri_document_id if ok else None,
                    "source": "agent_uri",
                    "is_revoke": None,
                    "revoke_at": None,
                    "feedback_created_at": None,
                    "processed_type": processed_type_for_uri(uri),
                    "is_active": ok,
                    "url_type": result.used_gateway or ("error" if not ok else "valid"),
                    "has_download_error": not ok,
                    "download_error_message": (
                        None
                        if ok
                        else f"{result.error or 'resolve_failed'} uri={truncate_uri_for_error(uri)}"
                    ),
                }
            )
            if ok and isinstance(result.document, dict):
                did_uri = extract_did_uri(result.document)
                if did_uri:
                    did_uri = normalize_did_uri(did_uri)
                    did = await resolver.resolve(did_uri)
                    did_ok = did.ok and did.document is not None
                    db.upsert_manifest(
                        {
                            "agent_id": agent_id,
                            "provider": "erc-8004-did",
                            "uri_document_id": did.uri_document_id if did_ok else None,
                            "source": "agent_uri_did",
                            "is_revoke": None,
                            "revoke_at": None,
                            "feedback_created_at": None,
                            "processed_type": "did_document",
                            "is_active": did_ok,
                            "url_type": did.used_gateway or ("error" if not did_ok else "valid"),
                            "has_download_error": not did_ok,
                            "download_error_message": (
                                None
                                if did_ok
                                else (
                                    f"{did.error or 'did_resolve_failed'} "
                                    f"uri={truncate_uri_for_error(did_uri)}"
                                )
                            ),
                        }
                    )
            logger.info(
                "Agent id=%s ok=%s gateway=%s",
                agent_id,
                ok,
                result.used_gateway,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Agent id=%s failed: %s", agent_id, exc)
            try:
                db.upsert_manifest(
                    {
                        "agent_id": agent_id,
                        "provider": "erc-8004",
                        "uri_document_id": None,
                        "source": "agent_uri",
                        "is_revoke": None,
                        "revoke_at": None,
                        "feedback_created_at": None,
                        "processed_type": processed_type_for_uri(uri) if uri else "exception",
                        "is_active": False,
                        "url_type": "exception",
                        "has_download_error": True,
                        "download_error_message": (
                            f"{exc} uri={truncate_uri_for_error(uri)}"
                        ),
                    }
                )
            except Exception:
                logger.exception("Failed to persist agent error id=%s", agent_id)


async def process_feedback(
    db: Database,
    resolver: UriResolver,
    row: dict[str, Any],
    sem: asyncio.Semaphore,
) -> None:
    async with sem:
        feedback_id = int(row["id"])
        agent_id = int(row["agent_id"])
        provider = f"feedback_erc_8004_id_{feedback_id}"
        uri, source = feedback_uri_and_source(row)
        try:
            if not uri:
                raise ValueError("No valid URI found in feedback")
            result = await resolver.resolve(uri)
            ok = result.ok and result.document is not None
            db.upsert_manifest(
                {
                    "agent_id": agent_id,
                    "provider": provider,
                    "uri_document_id": result.uri_document_id if ok else None,
                    "source": source,
                    "is_revoke": row.get("is_revoked"),
                    "revoke_at": row.get("revoked_at"),
                    "feedback_created_at": row.get("on_chain_created_at"),
                    "processed_type": row.get("feedback_type") or "feedback_uri",
                    "is_active": ok,
                    "url_type": result.used_gateway or ("error" if not ok else "valid"),
                    "has_download_error": not ok,
                    "download_error_message": (
                        None
                        if ok
                        else f"{result.error or 'resolve_failed'} uri={truncate_uri_for_error(uri)}"
                    ),
                }
            )
            logger.info(
                "Feedback id=%s ok=%s gateway=%s",
                feedback_id,
                ok,
                result.used_gateway,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Feedback id=%s failed: %s", feedback_id, exc)
            try:
                fallback_uri = uri or (
                    row.get("feedback_uri_raw") or row.get("end_point") or "unknown"
                )
                db.upsert_manifest(
                    {
                        "agent_id": agent_id,
                        "provider": provider,
                        "uri_document_id": None,
                        "source": "error",
                        "is_revoke": row.get("is_revoked"),
                        "revoke_at": row.get("revoked_at"),
                        "feedback_created_at": row.get("on_chain_created_at"),
                        "processed_type": row.get("feedback_type") or "feedback_uri",
                        "is_active": False,
                        "url_type": "exception",
                        "has_download_error": True,
                        "download_error_message": (
                            f"{exc} uri={truncate_uri_for_error(str(fallback_uri))}"
                        ),
                    }
                )
            except Exception:
                logger.exception("Failed to persist feedback error id=%s", feedback_id)


def _on_chain_document(row: dict[str, Any]) -> Any:
    doc = row.get("registration_feedback_json")
    if doc is None:
        raise ValueError("registration_feedback_json is empty")
    if isinstance(doc, str):
        doc = json.loads(doc) if doc.strip() else None
    if not doc or doc == {}:
        raise ValueError("registration_feedback_json is empty")
    return doc


def process_feedback_on_chain(db: Database, row: dict[str, Any]) -> None:
    """Materialize feedback_on_chain into uri_documents + agent_manifest (no fetch)."""
    feedback_id = int(row["id"])
    agent_id = int(row["agent_id"])
    provider = f"feedback_erc_8004_id_{feedback_id}"
    synthetic_uri = f"internal_on_chain_id_{feedback_id}"
    try:
        document = _on_chain_document(row)
        doc_id = db.upsert_document(
            uri=synthetic_uri,
            document=document,
            source_gateway="on_chain",
            cid=None,
        )
        db.upsert_manifest(
            {
                "agent_id": agent_id,
                "provider": provider,
                "uri_document_id": doc_id,
                "source": "on_chain",
                "is_revoke": row.get("is_revoked"),
                "revoke_at": row.get("revoked_at"),
                "feedback_created_at": row.get("on_chain_created_at"),
                "processed_type": "feedback_on_chain",
                "is_active": True,
                "url_type": "on_chain",
                "has_download_error": False,
                "download_error_message": None,
            }
        )
        logger.info("On-chain feedback id=%s ok=True", feedback_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("On-chain feedback id=%s failed: %s", feedback_id, exc)
        try:
            db.upsert_manifest(
                {
                    "agent_id": agent_id,
                    "provider": provider,
                    "uri_document_id": None,
                    "source": "on_chain",
                    "is_revoke": row.get("is_revoked"),
                    "revoke_at": row.get("revoked_at"),
                    "feedback_created_at": row.get("on_chain_created_at"),
                    "processed_type": "feedback_on_chain",
                    "is_active": False,
                    "url_type": "on_chain_exception",
                    "has_download_error": True,
                    "download_error_message": (
                        f"{exc} uri={truncate_uri_for_error(synthetic_uri)}"
                    ),
                }
            )
        except Exception:
            logger.exception(
                "Failed to persist on-chain feedback error id=%s", feedback_id
            )


async def run_job() -> int:
    load_dotenv_if_present()
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        logger.error("SUPABASE_DB_URL is required")
        return 1

    pinata = env_str("PINATA_GATEWAY")
    scrape_do = env_str("SCRAPE_DO_TOKEN")
    claim_batch = env_int("CLAIM_BATCH_SIZE", 20, minimum=1, maximum=200)
    concurrency = env_int("CONCURRENCY", 4, minimum=1, maximum=20)
    max_runtime = env_int("MAX_RUNTIME_SECONDS", 19800, minimum=60)
    worker_id = env_str("WORKER_ID", "resolve-a")

    db = Database(dsn)
    db.connect()
    started = time.monotonic()
    logger.info(
        "Started worker_id=%s claim_batch=%s concurrency=%s max_runtime=%ss",
        worker_id,
        claim_batch,
        concurrency,
        max_runtime,
    )

    total_agents = 0
    total_on_chain = 0
    total_feedbacks = 0

    try:
        async with httpx.AsyncClient() as http:
            resolver = UriResolver(
                db, http, pinata_token=pinata, scrape_do_token=scrape_do
            )
            sem = asyncio.Semaphore(concurrency)

            while True:
                elapsed = time.monotonic() - started
                if elapsed >= max_runtime:
                    logger.info("Time budget reached (%.0fs)", elapsed)
                    break

                try:
                    agents = db.claim_agents(claim_batch)
                except Exception as exc:
                    logger.warning(
                        "Claim agents failed; retry next loop: %s",
                        exc,
                    )
                    await asyncio.sleep(CLAIM_RETRY_BASE_SECONDS)
                    continue

                if agents:
                    logger.info("Claimed agents batch size=%s", len(agents))
                    await asyncio.gather(
                        *[process_agent(db, resolver, row, sem) for row in agents]
                    )
                    total_agents += len(agents)
                    continue

                try:
                    on_chain = db.claim_feedbacks_on_chain(claim_batch)
                except Exception as exc:
                    logger.warning(
                        "Claim on-chain feedbacks failed; retry next loop: %s",
                        exc,
                    )
                    await asyncio.sleep(CLAIM_RETRY_BASE_SECONDS)
                    continue

                if on_chain:
                    logger.info("Claimed on-chain feedbacks batch size=%s", len(on_chain))
                    for row in on_chain:
                        process_feedback_on_chain(db, row)
                    total_on_chain += len(on_chain)
                    continue

                try:
                    feedbacks = db.claim_feedbacks(claim_batch)
                except Exception as exc:
                    logger.warning("Claim feedbacks failed; retry next loop: %s", exc)
                    await asyncio.sleep(CLAIM_RETRY_BASE_SECONDS)
                    continue

                if feedbacks:
                    logger.info("Claimed feedbacks batch size=%s", len(feedbacks))
                    await asyncio.gather(
                        *[
                            process_feedback(db, resolver, row, sem)
                            for row in feedbacks
                        ]
                    )
                    total_feedbacks += len(feedbacks)
                    continue

                logger.info("No pending agents/on-chain/feedbacks; exiting")
                break

    except Exception:
        logger.error("Critical job failure\n%s", traceback.format_exc())
        return 1
    finally:
        db.close()

    logger.info(
        "Done agents=%s on_chain=%s feedbacks=%s elapsed=%.1fs",
        total_agents,
        total_on_chain,
        total_feedbacks,
        time.monotonic() - started,
    )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run_job()))


if __name__ == "__main__":
    main()
