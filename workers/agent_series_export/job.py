#!/usr/bin/env python3
"""Publica el arbol de series de 30 dias de cada agente en Supabase Storage."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
import time
import traceback
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from db import CLAIM_RETRY_BASE_SECONDS, Database
from storage import StorageClient, StorageError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("agent_series_export")

CLAIMED_BY_PREFIX = "agent_series_export/gha"
DEFAULT_BUCKET = "agent-series"
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


def env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def build_claimed_by(worker_suffix: str) -> str:
    suffix = worker_suffix.strip() or "exporter-a"
    if suffix.startswith(CLAIMED_BY_PREFIX):
        return suffix
    return f"{CLAIMED_BY_PREFIX}:{suffix}"


def object_path(agent_id: int) -> str:
    return f"agents/{agent_id}.json"


def content_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def refresh_scalars(db: Database, as_of: date, batch: int, deadline: float) -> bool:
    """Paso 1: recorre agents por cursor hasta que no quede nada por escanear."""
    cursor = 0
    scanned_total = 0
    upserted_total = 0
    started = time.monotonic()

    while True:
        if time.monotonic() >= deadline:
            logger.warning(
                "Scalars refresh out of time at agent_id=%s scanned=%s upserted=%s",
                cursor,
                scanned_total,
                upserted_total,
            )
            return False

        result = db.refresh_scalars(as_of, batch, cursor)
        scanned = int(result["agents_scanned"])
        if scanned == 0:
            logger.info(
                "Scalars refresh done scanned=%s upserted=%s elapsed=%.0fs",
                scanned_total,
                upserted_total,
                time.monotonic() - started,
            )
            return True

        cursor = int(result["last_agent_id"])
        scanned_total += scanned
        upserted_total += int(result["rows_upserted"])
        logger.info(
            "Scalars batch scanned=%s upserted=%s cursor=%s total_upserted=%s",
            scanned,
            result["rows_upserted"],
            cursor,
            upserted_total,
        )


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

    claimed_by = build_claimed_by(env_str("WORKER_ID", "exporter-a"))
    bucket = env_str("AGENT_SERIES_BUCKET", DEFAULT_BUCKET)
    concurrency = env_int("CONCURRENCY", default=16, minimum=1, maximum=64)
    claim_batch_size = env_int("CLAIM_BATCH_SIZE", default=500, minimum=1, maximum=2000)
    claim_stale_seconds = env_int("CLAIM_STALE_SECONDS", default=7200, minimum=60)
    scalars_batch_size = env_int("SCALARS_BATCH_SIZE", default=5000, minimum=100)
    max_runtime_seconds = env_int("MAX_RUNTIME_SECONDS", default=19800, minimum=60)
    # Solo una lane refresca escalares: el paso 1 ya es set-based y correrlo en las
    # dos duplicaria el trabajo sin acelerar nada.
    run_scalars = env_flag("RUN_SCALARS", default=False)

    db = Database(dsn)
    db.connect()

    as_of = db.resolve_as_of()
    cycle = db.cycle_open(as_of)
    logger.info(
        "Started claimed_by=%s as_of=%s bucket=%s run_scalars=%s concurrency=%s "
        "claim_batch_size=%s claim_stale_seconds=%s max_runtime=%ss cycle_started_at=%s",
        claimed_by,
        as_of,
        bucket,
        run_scalars,
        concurrency,
        claim_batch_size,
        claim_stale_seconds,
        max_runtime_seconds,
        cycle.get("started_at"),
    )

    start = time.monotonic()
    deadline = start + max_runtime_seconds
    processed = 0
    uploaded = 0
    skipped_empty = 0
    errors = 0
    failed_batches = 0
    queue_drained = False
    sem = asyncio.Semaphore(concurrency)
    http_limits = httpx.Limits(
        max_connections=concurrency * 2, max_keepalive_connections=concurrency
    )

    try:
        if run_scalars:
            refresh_scalars(db, as_of, scalars_batch_size, deadline)

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
                        "skipped_empty=%s errors=%s",
                        elapsed,
                        processed,
                        uploaded,
                        skipped_empty,
                        errors,
                    )
                    break

                try:
                    rows = db.claim_rows(
                        as_of=as_of,
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
                    queue_drained = True
                    break

                agent_ids = [int(row["agent_id"]) for row in rows]
                logger.info(
                    "Claimed batch size=%s first=%s last=%s",
                    len(agent_ids),
                    agent_ids[0],
                    agent_ids[-1],
                )

                async def publish(row: dict) -> tuple[dict | None, bool]:
                    """Devuelve (fila para ack, hubo upload)."""
                    agent_id = int(row["agent_id"])
                    document = row["document"]

                    # Sin wallet valida o sin filas en wallet_transactions: el agente
                    # se ackea igual para que salga de la cola, pero no gasta un PUT.
                    if document is None:
                        return {"agent_id": agent_id, "sha256": None}, False

                    payload = document.encode("utf-8")
                    sha256 = content_sha256(payload)

                    async with sem:
                        try:
                            await storage.upload(object_path(agent_id), payload)
                        except StorageError as exc:
                            logger.warning("agent_id=%s upload failed: %s", agent_id, exc)
                            return None, False

                    return {"agent_id": agent_id, "sha256": sha256}, True

                outcomes = await asyncio.gather(
                    *(publish(row) for row in rows), return_exceptions=True
                )

                done_rows: list[dict] = []
                failed_ids: list[int] = []
                batch_uploaded = 0
                batch_empty = 0
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
                    acked, did_upload = outcome
                    if acked is None:
                        failed_ids.append(int(row["agent_id"]))
                        continue
                    done_rows.append(acked)
                    if did_upload:
                        batch_uploaded += 1
                    else:
                        batch_empty += 1

                processed += len(done_rows)
                uploaded += batch_uploaded
                skipped_empty += batch_empty
                errors += len(failed_ids)

                if done_rows:
                    db.ack(done_rows, as_of)

                # Las fallidas conservan el soft-lock a proposito: liberarlas haria
                # que el claim siguiente devuelva las mismas y el worker no avance.
                # Vuelven a la cola solas al vencer CLAIM_STALE_SECONDS.
                if done_rows:
                    failed_batches = 0
                else:
                    failed_batches += 1

                logger.info(
                    "Batch done uploaded=%s skipped_empty=%s failed=%s total_processed=%s",
                    batch_uploaded,
                    batch_empty,
                    len(failed_ids),
                    processed,
                )

                if failed_batches >= MAX_CONSECUTIVE_FAILED_BATCHES:
                    logger.error(
                        "Aborting: %s consecutive batches with zero successful rows",
                        failed_batches,
                    )
                    return 1

        # El cierre se pide siempre: cuenta la cola real y deja el ciclo en closed
        # solo si no quedo ningun agente pendiente, sea esta lane o la otra.
        summary = db.cycle_close(as_of)
        logger.info(
            "Cycle as_of=%s status=%s exported=%s with_object=%s pending=%s scalars_ready=%s",
            summary.get("as_of"),
            summary.get("status"),
            summary.get("agents_exported"),
            summary.get("agents_with_object"),
            summary.get("agents_pending"),
            summary.get("scalars_ready"),
        )

    except Exception:
        logger.error("Critical job failure:\n%s", traceback.format_exc())
        return 1
    finally:
        db.close()

    logger.info(
        "Finished claimed_by=%s as_of=%s drained=%s processed=%s uploaded=%s "
        "skipped_empty=%s errors=%s elapsed=%.0fs",
        claimed_by,
        as_of,
        queue_drained,
        processed,
        uploaded,
        skipped_empty,
        errors,
        time.monotonic() - start,
    )
    return 0


def main() -> None:
    load_dotenv_if_present()
    raise SystemExit(asyncio.run(run_job()))


if __name__ == "__main__":
    main()
