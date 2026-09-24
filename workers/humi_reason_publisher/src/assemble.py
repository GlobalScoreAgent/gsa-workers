"""Ensamblado y serializacion del documento narrativo HUMI."""

from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal
from typing import Any

from pillar_spec import PILLARS, Item, Pillar

SCHEMA_VERSION = 1

_RENDERERS: dict[str, Any] | None = None


def render_mode_enabled() -> bool:
    raw = os.environ.get("HUMI_REASON_RENDER", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _load_renderers() -> dict[str, Any]:
    global _RENDERERS
    if _RENDERERS is not None:
        return _RENDERERS
    from render import (
        render_history_reasons,
        render_information_reasons,
        render_measures_reasons,
        render_usage_reasons,
    )

    _RENDERERS = {
        "pillar_history": render_history_reasons,
        "pillar_information": render_information_reasons,
        "pillar_measures": render_measures_reasons,
        "pillar_usage": render_usage_reasons,
    }
    return _RENDERERS


def _number(value: Any) -> Any:
    """Decimal -> int/float. La web parsea todo con Number(), asi que 5.00 y 5 son
    equivalentes; se emite la forma corta."""
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    return value


def _items(pillar_row: dict[str, Any] | None, items: tuple[Item, ...]) -> list[dict[str, Any]]:
    row = pillar_row or {}
    return [
        {
            "name": item.name,
            "points": _number(row.get(item.score_column)),
            "reason": row.get(item.reason_column),
        }
        for item in items
    ]


def build_pillar_summary(pillar: Pillar, pillar_row: dict[str, Any] | None) -> dict[str, Any]:
    """Replica jsonb_build_object del CTE `pillars` de agent_index_humi_calculate.

    Cuando el agente no tiene fila en la tabla del pilar, SQL igual construye el
    objeto con nulls (LEFT JOIN + jsonb_build_object nunca devuelve NULL), asi que
    aca se hace lo mismo en vez de omitir la clave.
    """
    row = pillar_row or {}
    return {
        "block_basic_score": _number(row.get("block_basic_score")),
        "block_basic_items": _items(pillar_row, pillar.basic),
        "block_intermediate_score": _number(row.get("block_intermediate_score")),
        "block_intermediate_items": _items(pillar_row, pillar.intermediate),
        "block_advanced_score": _number(row.get("block_advanced_score")),
        "block_advanced_items": _items(pillar_row, pillar.advanced),
        "summary": row.get("pillar_summary"),
    }


def _scores_only(pillar: Pillar, pillar_row: dict[str, Any] | None) -> dict[str, Any]:
    row = pillar_row or {}
    out: dict[str, Any] = {}
    for col in (
        "block_basic_score",
        "block_intermediate_score",
        "block_advanced_score",
        "pillar_score",
    ):
        if col in row:
            out[col] = row[col]
    for item in (*pillar.basic, *pillar.intermediate, *pillar.advanced):
        if item.score_column in row:
            out[item.score_column] = row[item.score_column]
    return out


def apply_render(
    pillar: Pillar,
    pillar_row: dict[str, Any] | None,
    ctx: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Fusiona scores de la fila con reasons generados en Python."""
    if pillar_row is None:
        return None
    renderers = _load_renderers()
    renderer = renderers[pillar.table]
    rendered = renderer(ctx or {}, _scores_only(pillar, pillar_row))
    merged = dict(pillar_row)
    for key, value in rendered.items():
        merged[key] = value
    return merged


def build_document(
    agent_id: int,
    pillar_rows: dict[str, dict[str, Any] | None],
    *,
    render_contexts: dict[str, dict[str, Any]] | None = None,
    use_render: bool | None = None,
) -> dict[str, Any]:
    """pillar_rows viene indexado por nombre de tabla (pillar_history, ...).

    Si use_render (o HUMI_REASON_RENDER), genera reasons via src/render/ usando
    render_contexts[table] como inputs; si no, copia *_reason de la fila (Etapa 1).
    """
    if use_render is None:
        use_render = render_mode_enabled()
    contexts = render_contexts or {}
    document: dict[str, Any] = {"schema": SCHEMA_VERSION, "agent_id": agent_id}
    for pillar in PILLARS:
        row = pillar_rows.get(pillar.table)
        if use_render and row is not None:
            row = apply_render(pillar, row, contexts.get(pillar.table))
        document[pillar.output_key] = build_pillar_summary(pillar, row)
    return document


def normalize_summary_for_parity(summary: Any) -> Any:
    """Quita last_calculated para comparar paridad Stage 2 vs SQL."""
    if not isinstance(summary, dict):
        return summary
    out = dict(summary)
    out.pop("last_calculated", None)
    return out


def serialize(document: dict[str, Any]) -> bytes:
    """Serializacion determinista: el sha256 del resultado decide si hay upload.

    El documento no lleva timestamp a proposito; si llevara, cada corrida
    generaria un hash distinto y el short-circuit no ahorraria nada.
    """
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def content_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def object_path(agent_id: int) -> str:
    return f"humi/agent/{agent_id}.json"
