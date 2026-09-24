#!/usr/bin/env python3
"""Publica el agregado narrativo HUMI de cada agente en Supabase Storage."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import traceback
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from assemble import (
    build_document,
    content_sha256,
    object_path,
    render_mode_enabled,
    serialize,
)
from db import CLAIM_RETRY_BASE_SECONDS, Database
from storage import StorageClient, StorageError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("humi_reason_publisher")

CLAIMED_BY_PREFIX = "humi_reason_publisher/gha"
DEFAULT_BUCKET = "humi-reasons"
# Corta la corrida si el problema es sistemico (key vencida, bucket borrado) en vez
# de quemar las 5.5h fallando lote tras lote.
MAX_CONSECUTIVE_FAILED_BATCHES = 3


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
    if raw is None or raw.strip() == "":
        value = default
    else:
        value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def build_claimed_by(worker_suffix: str) -> str:
    suffix = worker_suffix.strip() or "publisher-a"
    if suffix.startswith(CLAIMED_BY_PREFIX):
        return suffix
    return f"{CLAIMED_BY_PREFIX}:{suffix}"


async def run_job() -> int:
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        logger.error("SUPABASE_DB_URL is required")
        return 1
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    if not supabase_url:
        logger.error("SUPABASE_URL is required")
        return 1
    service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not service_role_key:
        logger.error("SUPABASE_SERVICE_ROLE_KEY is required")
        return 1

    claimed_by = build_claimed_by(env_str("WORKER_ID", "publisher-a"))
    bucket = env_str("HUMI_REASON_BUCKET", DEFAULT_BUCKET)
    concurrency = env_int("CONCURRENCY", default=16, minimum=1, maximum=64)
    claim_batch_size = env_int("CLAIM_BATCH_SIZE", default=500, minimum=1, maximum=5000)
    claim_stale_seconds = env_int("CLAIM_STALE_SECONDS", default=7200, minimum=60)
    max_runtime_seconds = env_int("MAX_RUNTIME_SECONDS", default=19800, minimum=60)
    use_render = render_mode_enabled()

    db = Database(dsn)
    db.connect()
    logger.info(
        "Started claimed_by=%s bucket=%s concurrency=%s claim_batch_size=%s "
        "claim_stale_seconds=%s max_runtime=%ss humi_reason_render=%s",
        claimed_by,
        bucket,
        concurrency,
        claim_batch_size,
        claim_stale_seconds,
        max_runtime_seconds,
        use_render,
    )

    start = time.monotonic()
    processed = 0
    uploaded = 0
    unchanged = 0
    errors = 0
    failed_batches = 0
    sem = asyncio.Semaphore(concurrency)
    http_limits = httpx.Limits(
        max_connections=concurrency * 2, max_keepalive_connections=concurrency
    )

    try:
        async with httpx.AsyncClient(
            limits=http_limits,
            timeout=httpx.Timeout(60.0),
        ) as http_client:
            storage = StorageClient(
                http_client,
                base_url=supabase_url,
                service_role_key=service_role_key,
                bucket=bucket,
            )

            while True:
                elapsed = time.monotonic() - start
                if elapsed >= max_runtime_seconds:
                    logger.info(
                        "Time budget reached (%.0fs). processed=%s uploaded=%s "
                        "unchanged=%s errors=%s",
                        elapsed,
                        processed,
                        uploaded,
                        unchanged,
                        errors,
                    )
                    break

                try:
                    rows = db.claim_rows(
                        worker_id=claimed_by,
                        limit=claim_batch_size,
                        stale_seconds=claim_stale_seconds,
                    )
                except Exception as exc:
                    logger.error("Claim failed; will retry next loop: %s", exc)
                    await asyncio.sleep(CLAIM_RETRY_BASE_SECONDS)
                    continue

                if not rows:
                    logger.info("queue empty")
                    break

                agent_ids = [int(row["agent_id"]) for row in rows]
                logger.info(
                    "Claimed batch size=%s first=%s last=%s",
                    len(agent_ids),
                    agent_ids[0],
                    agent_ids[-1],
                )

                try:
                    pillars_by_agent = db.fetch_pillars(
                        agent_ids, include_reasons=not use_render
                    )
                    contexts_by_agent: dict[int, dict[str, dict]] = {}
                    if use_render:
                        contexts_by_agent = db.fetch_render_contexts(agent_ids)
                except Exception as exc:
                    # No se libera el soft-lock: si se liberara, el claim siguiente
                    # devolveria exactamente estas filas y el worker giraria en vacio.
                    # Quedan bloqueadas hasta CLAIM_STALE_SECONDS y la corrida avanza.
                    logger.error("fetch_pillars failed for batch: %s", exc)
                    errors += len(agent_ids)
                    failed_batches += 1
                    if failed_batches >= MAX_CONSECUTIVE_FAILED_BATCHES:
                        logger.error(
                            "Aborting: %s consecutive failed batches", failed_batches
                        )
                        return 1
                    continue

                async def publish(row: dict) -> tuple[dict | None, bool]:
                    """Devuelve (fila para complete, hubo upload)."""
                    agent_id = int(row["agent_id"])
                    document = build_document(
                        agent_id,
                        pillars_by_agent.get(agent_id, {}),
                        render_contexts=contexts_by_agent.get(agent_id),
                        use_render=use_render,
                    )
                    payload = serialize(document)
                    sha256 = content_sha256(payload)

                    if sha256 == row.get("reason_content_sha256"):
                        # El texto no cambio desde la ultima publicacion: se limpia
                        # el flag sin gastar un PUT contra Storage.
                        return {
                            "agent_id": agent_id,
                            "version": row["version"],
                            "sha256": sha256,
                        }, False

                    async with sem:
                        try:
                            await storage.upload(object_path(agent_id), payload)
                        except StorageError as exc:
                            logger.warning("agent_id=%s upload failed: %s", agent_id, exc)
                            return None, False

                    return {
                        "agent_id": agent_id,
                        "version": row["version"],
                        "sha256": sha256,
                    }, True

                outcomes = await asyncio.gather(
                    *(publish(row) for row in rows), return_exceptions=True
                )

                done_rows: list[dict] = []
                failed_ids: list[int] = []
                batch_uploaded = 0
                batch_unchanged = 0
                for row, outcome in zip(rows, outcomes):
                    if isinstance(outcome, BaseException):
                        logger.warning(
                            "agent_id=%s raised %s: %s",
                            row["agent_id"],
                            outcome.__class__.__name__,
                            outcome,
                        )
                        failed_ids.append(int(row["agent_id"]))
                        continue
                    completed, did_upload = outcome
                    if completed is None:
                        failed_ids.append(int(row["agent_id"]))
                        continue
                    done_rows.append(completed)
                    if did_upload:
                        batch_uploaded += 1
                    else:
                        batch_unchanged += 1

                processed += len(done_rows)
                uploaded += batch_uploaded
                unchanged += batch_unchanged
                errors += len(failed_ids)

                if done_rows:
                    db.complete(done_rows)

                # Las fallidas conservan el soft-lock a proposito: liberarlas haria
                # que el claim siguiente devuelva las mismas y el worker no avance.
                # Vuelven a la cola solas al vencer CLAIM_STALE_SECONDS.
                if done_rows:
                    failed_batches = 0
                else:
                    failed_batches += 1

                logger.info(
                    "Batch done uploaded=%s unchanged=%s failed=%s total_processed=%s",
                    batch_uploaded,
                    batch_unchanged,
                    len(failed_ids),
                    processed,
                )

                if failed_batches >= MAX_CONSECUTIVE_FAILED_BATCHES:
                    logger.error(
                        "Aborting: %s consecutive batches with zero successful rows",
                        failed_batches,
                    )
                    return 1

    except Exception:
        logger.error("Critical job failure:\n%s", traceback.format_exc())
        return 1
    finally:
        db.close()

    logger.info(
        "Finished claimed_by=%s processed=%s uploaded=%s unchanged=%s errors=%s elapsed=%.0fs",
        claimed_by,
        processed,
        uploaded,
        unchanged,
        errors,
        time.monotonic() - start,
    )
    return 0


def main() -> None:
    load_dotenv_if_present()
    raise SystemExit(asyncio.run(run_job()))


if __name__ == "__main__":
    main()
