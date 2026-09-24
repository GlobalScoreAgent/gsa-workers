"""Estructura del agregado narrativo HUMI, tal como lo arma hoy SQL.

Fuente: index_humi.agent_index_humi_calculate, CTE `pillars`. Cada pilar produce
un objeto con tres bloques, y cada bloque una lista de items {name, points, reason}.

Este modulo es la unica fuente de verdad del mapeo columna -> item: la consulta a
las tablas pillar_* y el ensamblado del documento se derivan de aca, asi que no
puede haber desfase entre lo que se lee y lo que se publica.

En la Etapa 2 el worker deja de leer las columnas `*_reason` y pasa a renderizar
el texto; la forma del documento no cambia.
"""

from __future__ import annotations

from typing import NamedTuple


class Item(NamedTuple):
    """Un sub-item puntuable dentro de un bloque."""

    name: str
    score_column: str
    reason_column: str


class Pillar(NamedTuple):
    table: str
    output_key: str
    basic: tuple[Item, ...]
    intermediate: tuple[Item, ...]
    advanced: tuple[Item, ...]


def _item(name: str, prefix: str) -> Item:
    return Item(name, f"{prefix}_score", f"{prefix}_reason")


HISTORY = Pillar(
    table="pillar_history",
    output_key="pillar_history_summary",
    basic=(
        _item("Owner Wallet Active", "owner_wallet_active"),
        _item("Ownership Stability", "agent_owner_stability"),
    ),
    intermediate=(
        _item("Owner Advanced Antiquity", "owner_antiquity"),
        _item("Active Agents in Portfolio", "portafolio_agent_active"),
        _item("Minimum External Audit", "portafolio_basic_external_audits"),
        _item("No External Warnings", "portafolio_warnings"),
    ),
    advanced=(
        _item("Good Metadata + Services", "portafolio_quality_agent"),
        _item("Advanced External Audits", "portafolio_advanced_external_audits"),
        _item("General Portfolio Activity", "portafolio_general_activity"),
    ),
)

INFORMATION = Pillar(
    table="pillar_information",
    output_key="pillar_information_summary",
    basic=(
        _item("Name", "name"),
        _item("Description", "description"),
        _item("Image", "image"),
        _item("Basic Sources", "profiles"),
    ),
    intermediate=(
        _item("External Sources / Diversity", "extended_profiles"),
        _item("Web or Email", "basic_contact"),
        _item("Programmatic / API", "extended_contact"),
        _item("Supported Trust", "supported_trust"),
        _item("Verification Methods", "verification_methods"),
        _item("Basic Technical Metadata", "basic_technical_metadata"),
    ),
    advanced=(
        _item("MCP Endpoint", "mcp_endpoint"),
        _item("A2A Endpoint", "a2a_endpoint"),
        _item("Advanced Technical Setup", "advanced_technical_metadata"),
    ),
)

MEASURES = Pillar(
    table="pillar_measures",
    output_key="pillar_measure_summary",
    basic=(
        _item("Metadata Richness", "metadata_richness"),
        _item("Basic Existence", "existence"),
    ),
    intermediate=(
        _item("Wallet Transaction Quality", "intermediate_wallet_transaction"),
        _item("External Audit", "intermediate_external_audits"),
        _item("Protocol Activity", "intermediate_protocol_activities"),
        _item("Multichain Presence Quality", "multichain_presence_quality"),
    ),
    advanced=(
        _item("External Audit (Advanced)", "advanced_external_audits"),
        _item("Identity Analysis", "identity_analysis"),
        _item("Protocol Activity (Advanced)", "advanced_protocol_activities"),
    ),
)

USAGE = Pillar(
    table="pillar_usage",
    output_key="pillar_usage_summary",
    basic=(_item("Basic General Activity", "basic_activity"),),
    intermediate=(
        _item("Wallet Intermediate", "wallet_intermediate_activity"),
        _item("On-Chain Activity Intermediate", "on_chain_intermediate_activity"),
        _item("Comments", "comment"),
        _item("Multichain Valuable Usage", "multichain_valuable_usage"),
    ),
    advanced=(
        _item("Wallet Advanced", "wallet_advanced_activity"),
        _item("On-Chain Activity Advanced", "on_chain_advanced_activity"),
        _item("Protocol Activity with Payments", "on_chain_activity_paid"),
        _item("Multichain High Value Consistency", "multichain_high_value_consistency"),
    ),
)

PILLARS: tuple[Pillar, ...] = (HISTORY, INFORMATION, MEASURES, USAGE)

BLOCK_SCORE_COLUMNS = ("block_basic_score", "block_intermediate_score", "block_advanced_score")


def pillar_columns(pillar: Pillar, *, include_reasons: bool = True) -> list[str]:
    """Columnas que hay que leer de la tabla del pilar, sin agent_id.

    Stage 2 (HUMI_REASON_RENDER): include_reasons=False — solo scores; el texto
    lo genera src/render/.
    """
    columns = list(BLOCK_SCORE_COLUMNS)
    if include_reasons:
        columns.append("pillar_summary")
    for item in (*pillar.basic, *pillar.intermediate, *pillar.advanced):
        columns.append(item.score_column)
        if include_reasons:
            columns.append(item.reason_column)
    return columns


def pillar_score_columns(pillar: Pillar) -> list[str]:
    return pillar_columns(pillar, include_reasons=False)
