"""Paridad Stage 2: reasons renderizados vs SQL para agent_id=2 (history).

Correr: uv run python tests/test_render_parity.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from assemble import normalize_summary_for_parity
from render.history import render_history_reasons

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Scores + reasons SQL snapshot (agent 2, calculated ~2026-08-10).
HISTORY_SCORES = {
    "block_basic_score": 10,
    "block_intermediate_score": 6.5,
    "block_advanced_score": 0.75,
    "owner_wallet_active_score": 5,
    "agent_owner_stability_score": 5,
    "owner_antiquity_score": 2,
    "portafolio_agent_active_score": 3,
    "portafolio_basic_external_audits_score": 1.5,
    "portafolio_warnings_score": 0,
    "portafolio_quality_agent_score": 0.75,
    "portafolio_advanced_external_audits_score": 0,
    "portafolio_general_activity_score": 0,
    "pillar_score": 17.25,
}

HISTORY_CTX = {
    "owner_changes": 1,
    "on_chain_created_at": "2026-02-03T17:19:21+00:00",
    "wallet_created_at": "2026-02-03T17:34:02+00:00",
    "owner_portafolio_agent_total": 9,
    "owner_portafolio_agent_active_total": 9,
    "owner_portafolio_agent_metadata_richness": {"Moderate / Basic": 9},
    "owner_portafolio_agent_warnings": {"total_agents_with_warnings": 9},
    "owner_portafolio_agent_external_audits": {
        "total_valid_audits": 2,
        "agents_with_good_score": 0,
        "agents_with_excellent_score": 0,
    },
    "owner_portafolio_agent_attestations": {
        "agents_with_good_score": 0,
        "total_valid_attestations": 3,
        "agents_with_excellent_score": 0,
    },
    "owner_portafolio_agent_on_chain_executions": {"agents_with_executions": 0},
    "owner_portafolio_agent_activity_protocols": {"total_protocol_activities": 2},
    "owner_portafolio_agents_metadata_services": {
        "agents_with_one_service": 0,
        "agents_with_two_services": 0,
        "agents_with_four_services": 0,
        "agents_with_three_services": 0,
        "agents_with_specialized_services": 0,
        "agents_with_five_or_more_services": 9,
    },
    "owner_has_active_wallet_in_chain_info": True,
    "as_of": "2026-08-10",
}

EXPECTED_REASONS = {
    "owner_wallet_active_reason": {
        "reason_eng": (
            "Owner wallet is confirmed active or supports active agents in portfolio, "
            "demonstrating real participation and operational legitimacy."
        ),
        "reason_esp": (
            "La wallet del owner está confirmada como activa o soporta agentes activos "
            "en el portafolio, demostrando participación real y legitimidad operativa."
        ),
        "active_agents_in_portfolio": 9,
        "has_active_wallet_in_chain_info": True,
    },
    "agent_owner_stability_reason": {
        "reason_eng": (
            "Excellent ownership stability for a mature agent (≥6 months) with minimal "
            "changes, reflecting high trust and continuity."
        ),
        "reason_esp": (
            "Excelente estabilidad de propiedad para un agente maduro (≥6 meses) con "
            "cambios mínimos, reflejando alta confianza y continuidad."
        ),
        "owner_changes": 1,
        "agent_age_months": 6,
    },
    "owner_antiquity_reason": {
        "reason_eng": (
            "Owner shows good antiquity (1-2 years), indicating solid experience and "
            "moderate maturity."
        ),
        "reason_esp": (
            "El owner muestra buena antigüedad (1-2 años), indicando experiencia sólida "
            "y madurez moderada."
        ),
        "days_since_first_tx": 188,
    },
    "portafolio_agent_active_reason": {
        "reason_eng": (
            "Excellent portfolio health with ≥80% active agents, demonstrating strong "
            "owner management and operational consistency."
        ),
        "reason_esp": (
            "Salud del portafolio excelente con ≥80% de agentes activos, demostrando "
            "fuerte gestión del owner y consistencia operativa."
        ),
        "percentage_active": 100,
    },
    "portafolio_warnings_reason": {
        "reason_eng": (
            "Elevated warnings in portfolio (>10%), increasing perceived risk and "
            "potential compliance concerns."
        ),
        "reason_esp": (
            "Advertencias elevadas en el portafolio (>10%), aumentando el riesgo "
            "percibido y posibles preocupaciones de cumplimiento."
        ),
        "warnings_pct": 100,
    },
}


def _canon(obj: object) -> object:
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))


def test_history_leaf_reasons() -> None:
    rendered = render_history_reasons(HISTORY_CTX, HISTORY_SCORES)
    for key, expected in EXPECTED_REASONS.items():
        got = _canon(rendered[key])
        exp = _canon(expected)
        assert got == exp, f"{key} mismatch:\n got={got}\n exp={exp}"


def test_history_summary_shape() -> None:
    rendered = render_history_reasons(HISTORY_CTX, HISTORY_SCORES)
    summary = normalize_summary_for_parity(rendered["pillar_summary"])
    assert "last_calculated" not in (summary or {})
    assert summary["overall_assessment_eng"].startswith(
        "The agent has acceptable history"
    )
    assert "High warnings" in summary["business_interpretation_eng"]
    assert summary["key_strengths_eng"] == [
        "Proven owner longevity and ecosystem experience",
        "Healthy active portfolio with strong management",
    ]
    assert summary["main_concerns_eng"] == ["High number of warnings in the portfolio"]


def main() -> None:
    test_history_leaf_reasons()
    test_history_summary_shape()
    # Keep Stage 1 mapping test green too.
    from test_spec_parity import main as spec_main

    spec_main()
    print("OK render parity (history) + spec parity")


if __name__ == "__main__":
    main()
