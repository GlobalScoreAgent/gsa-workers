"""Paridad del spec contra el agregado que SQL guarda hoy en index_humi_agent.

Muestra real de prod (agent_id = 2, 2026-09-16). Verifica que el mapeo
nombre de item -> columna de score de pillar_spec coincide con el que arma
index_humi.agent_index_humi_calculate. Un typo en el spec (columna equivocada
bajo un nombre) rompe este test.

Correr: uv run python tests/test_spec_parity.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from assemble import build_pillar_summary
from pillar_spec import PILLARS

# Scores reales por tabla de pilar (agent_id = 2).
PILLAR_SCORES = {
    "pillar_history": {
        "block_basic_score": 10.0,
        "block_intermediate_score": 6.5,
        "block_advanced_score": 0.75,
        "owner_wallet_active_score": 5.0,
        "agent_owner_stability_score": 5.0,
        "owner_antiquity_score": 2.0,
        "portafolio_agent_active_score": 3.0,
        "portafolio_basic_external_audits_score": 1.5,
        "portafolio_warnings_score": 0,
        "portafolio_quality_agent_score": 0.75,
        "portafolio_advanced_external_audits_score": 0,
        "portafolio_general_activity_score": 0,
    },
    "pillar_information": {
        "block_basic_score": 10.0,
        "block_intermediate_score": 4.5,
        "block_advanced_score": 0,
        "name_score": 3.0,
        "description_score": 3.0,
        "image_score": 1.5,
        "profiles_score": 2.5,
        "extended_profiles_score": 0,
        "basic_contact_score": 1.0,
        "extended_contact_score": 0,
        "supported_trust_score": 1.0,
        "verification_methods_score": 1.0,
        "basic_technical_metadata_score": 1.5,
        "mcp_endpoint_score": 0,
        "a2a_endpoint_score": 0,
        "advanced_technical_metadata_score": 0,
    },
    "pillar_measures": {
        "block_basic_score": 8.52,
        "block_intermediate_score": -1.0,
        "block_advanced_score": 0,
        "metadata_richness_score": 2.52,
        "existence_score": 6.0,
        "intermediate_wallet_transaction_score": 0,
        "intermediate_external_audits_score": 0,
        "intermediate_protocol_activities_score": 0,
        "multichain_presence_quality_score": -1.0,
        "advanced_external_audits_score": 0,
        "identity_analysis_score": 0,
        "advanced_protocol_activities_score": 0,
    },
    "pillar_usage": {
        "block_basic_score": 10.0,
        "block_intermediate_score": -0.25,
        "block_advanced_score": 0,
        "basic_activity_score": 10.0,
        "wallet_intermediate_activity_score": 0.75,
        "on_chain_intermediate_activity_score": 0,
        "comment_score": 0,
        "multichain_valuable_usage_score": -1.0,
        "wallet_advanced_activity_score": 0,
        "on_chain_advanced_activity_score": 0,
        "on_chain_activity_paid_score": 0,
        "multichain_high_value_consistency_score": 0,
    },
}

# Lo que hoy tiene index_humi_agent.pillar_*_reason para ese agente:
# [nombre del item, puntos] por bloque, en orden.
EXPECTED = {
    "pillar_history_summary": {
        "scores": (10.0, 6.5, 0.75),
        "basic": [("Owner Wallet Active", 5.0), ("Ownership Stability", 5.0)],
        "intermediate": [
            ("Owner Advanced Antiquity", 2.0),
            ("Active Agents in Portfolio", 3.0),
            ("Minimum External Audit", 1.5),
            ("No External Warnings", 0),
        ],
        "advanced": [
            ("Good Metadata + Services", 0.75),
            ("Advanced External Audits", 0),
            ("General Portfolio Activity", 0),
        ],
    },
    "pillar_information_summary": {
        "scores": (10.0, 4.5, 0),
        "basic": [
            ("Name", 3.0),
            ("Description", 3.0),
            ("Image", 1.5),
            ("Basic Sources", 2.5),
        ],
        "intermediate": [
            ("External Sources / Diversity", 0),
            ("Web or Email", 1.0),
            ("Programmatic / API", 0),
            ("Supported Trust", 1.0),
            ("Verification Methods", 1.0),
            ("Basic Technical Metadata", 1.5),
        ],
        "advanced": [
            ("MCP Endpoint", 0),
            ("A2A Endpoint", 0),
            ("Advanced Technical Setup", 0),
        ],
    },
    "pillar_measure_summary": {
        "scores": (8.52, -1.0, 0),
        "basic": [("Metadata Richness", 2.52), ("Basic Existence", 6.0)],
        "intermediate": [
            ("Wallet Transaction Quality", 0),
            ("External Audit", 0),
            ("Protocol Activity", 0),
            ("Multichain Presence Quality", -1.0),
        ],
        "advanced": [
            ("External Audit (Advanced)", 0),
            ("Identity Analysis", 0),
            ("Protocol Activity (Advanced)", 0),
        ],
    },
    "pillar_usage_summary": {
        "scores": (10.0, -0.25, 0),
        "basic": [("Basic General Activity", 10.0)],
        "intermediate": [
            ("Wallet Intermediate", 0.75),
            ("On-Chain Activity Intermediate", 0),
            ("Comments", 0),
            ("Multichain Valuable Usage", -1.0),
        ],
        "advanced": [
            ("Wallet Advanced", 0),
            ("On-Chain Activity Advanced", 0),
            ("Protocol Activity with Payments", 0),
            ("Multichain High Value Consistency", 0),
        ],
    },
}


def main() -> int:
    failures: list[str] = []

    for pillar in PILLARS:
        built = build_pillar_summary(pillar, PILLAR_SCORES[pillar.table])
        expected = EXPECTED[pillar.output_key]

        got_scores = (
            built["block_basic_score"],
            built["block_intermediate_score"],
            built["block_advanced_score"],
        )
        if got_scores != expected["scores"]:
            failures.append(
                f"{pillar.output_key}: block scores {got_scores} != {expected['scores']}"
            )

        for block in ("basic", "intermediate", "advanced"):
            got = [(i["name"], i["points"]) for i in built[f"block_{block}_items"]]
            if got != expected[block]:
                failures.append(f"{pillar.output_key}.{block}: {got} != {expected[block]}")

    # Un agente sin filas de pilar debe producir el objeto con nulls, no ausencia
    # de claves: es lo que hace jsonb_build_object sobre un LEFT JOIN vacio.
    empty = build_pillar_summary(PILLARS[0], None)
    if empty["block_basic_score"] is not None:
        failures.append("pilar vacio: block_basic_score deberia ser null")
    if len(empty["block_basic_items"]) != len(PILLARS[0].basic):
        failures.append("pilar vacio: faltan items")
    if empty["block_basic_items"][0]["reason"] is not None:
        failures.append("pilar vacio: reason deberia ser null")

    for failure in failures:
        print(f"FAIL {failure}")
    if failures:
        return 1
    print(f"OK parity: {len(PILLARS)} pilares, mapeo nombre->columna identico a SQL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
