"""Ensamblado y serializacion del documento narrativo HUMI."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from pillar_spec import PILLARS, Item, Pillar

SCHEMA_VERSION = 1


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


def build_document(agent_id: int, pillar_rows: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    """pillar_rows viene indexado por nombre de tabla (pillar_history, ...)."""
    document: dict[str, Any] = {"schema": SCHEMA_VERSION, "agent_id": agent_id}
    for pillar in PILLARS:
        document[pillar.output_key] = build_pillar_summary(pillar, pillar_rows.get(pillar.table))
    return document


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
