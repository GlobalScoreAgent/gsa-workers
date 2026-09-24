"""Render de reasons + pillar_summary del pilar Measure (paridad SQL).

Fuente canónica: index_humi.agent_pillar_measure_calculate
(migración 00000000000050_agent_pillar_measure_multichain.sql /
scripts/agent_pillar_measure_calculate.sql).

No recalcula scores para decidir el texto cuando SQL hace CASE WHEN score = X:
usa el score persistido en `scores`. El control flow de IFs sobre inputs replica SQL.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

from pillar_spec import MEASURES

from .util import (
    as_mapping,
    build_reason,
    coalesce,
    jsonb_agg,
    mapping_get_num,
    score_eq,
    score_ge,
    score_lt,
    sql_round,
    to_decimal,
    whole_number,
)

logger = logging.getLogger("humi_reason_publisher.render.measure")


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "t", "1", "yes")
    return bool(value)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _mapping(ctx: dict[str, Any], key: str) -> dict[str, Any]:
    return as_mapping(ctx.get(key)) or {}


def _int_metric(value: Any, default: int = 0) -> int:
    return int(to_decimal(coalesce(value, default)))


# ---------------------------------------------------------------------------
# Basic
# ---------------------------------------------------------------------------


def render_metadata_richness_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    info = _mapping(ctx, "metadata_richness_information")
    richness_score = mapping_get_num(info, "score", 0) if info else to_decimal(
        ctx.get("metadata_richness_score"), 0
    )
    category = info.get("category") if info else None
    if category is None:
        category = ctx.get("metadata_category")
    category = coalesce(category, "unknown")
    if not isinstance(category, str) or not category:
        category = "unknown"

    expected = (richness_score / Decimal(100)) * Decimal(4)
    _verify_score("metadata_richness", score, expected)

    if category == "Excellent / Production-Ready":
        eng = (
            "Excellent metadata richness (85–100): production-ready profile with "
            "complete professional presence."
        )
        esp = (
            "Riqueza de metadatos excelente (85–100): perfil listo para producción "
            "con presencia profesional completa."
        )
    elif category == "Strong / Well-Developed":
        eng = (
            "Strong metadata richness (70–84): well-developed agent profile suitable "
            "for most real-world use."
        )
        esp = (
            "Riqueza de metadatos fuerte (70–84): perfil bien desarrollado apto para "
            "la mayoría de usos reales."
        )
    elif category == "Moderate / Basic":
        eng = (
            "Moderate metadata richness (50–69): functional but needs improvement in "
            "key metadata areas."
        )
        esp = (
            "Riqueza de metadatos moderada (50–69): funcional pero requiere mejoras "
            "en áreas clave de metadatos."
        )
    elif category == "Limited / Incomplete":
        eng = (
            "Limited metadata richness (below 50): incomplete profile requiring "
            "significant metadata work."
        )
        esp = (
            "Riqueza de metadatos limitada (menos de 50): perfil incompleto que "
            "requiere trabajo significativo en metadatos."
        )
    else:
        eng = (
            "Limited metadata richness: category unknown or insufficient; treated "
            "as incomplete profile."
        )
        esp = (
            "Riqueza de metadatos limitada: categoría desconocida o insuficiente; "
            "se trata como perfil incompleto."
        )

    return build_reason(
        eng,
        esp,
        richness_score=whole_number(richness_score),
        metadata_category=category,
    )


def render_existence_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    wallet = _mapping(ctx, "wallet_transaction_data")
    att = _mapping(ctx, "attestations_summary")
    execs = _mapping(ctx, "on_chain_executions_summary")
    fbs = _mapping(ctx, "on_chain_feedbacks_summary")

    nonce_current = _int_metric(wallet.get("nonce_current") if wallet else ctx.get("nonce_current"))
    att_valid = _int_metric(att.get("valid_count") if att else None)
    exec_valid = _int_metric(execs.get("valid_count") if execs else None)
    fb_valid = _int_metric(fbs.get("valid_count") if fbs else None)
    has_onchain_basic = att_valid > 0 or exec_valid > 0 or fb_valid > 0

    if nonce_current > 0 or has_onchain_basic:
        _verify_score("existence", score, Decimal("6"))
        return build_reason(
            "Clear evidence of real existence through wallet activity or on-chain "
            "signals (attestations, executions, feedbacks), establishing strong "
            "foundational legitimacy.",
            "Evidencia clara de existencia real mediante actividad en wallet o "
            "señales on-chain (attestations, executions, feedbacks), estableciendo "
            "una legitimidad fundamental sólida.",
            wallet_basic=nonce_current > 0,
            nonce_current=nonce_current,
            onchain_basic=has_onchain_basic,
            attestations_valid_count=att_valid,
            on_chain_executions_valid_count=exec_valid,
            on_chain_feedbacks_valid_count=fb_valid,
        )

    _verify_score("existence", score, 0)
    return build_reason(
        "No evidence of wallet activity or valid on-chain records, indicating "
        "potential lack of real operational existence.",
        "No se detecta evidencia de actividad en wallet ni registros on-chain "
        "válidos, lo que indica posible falta de existencia operativa real.",
        wallet_basic=False,
        nonce_current=nonce_current,
        onchain_basic=False,
        attestations_valid_count=att_valid,
        on_chain_executions_valid_count=exec_valid,
        on_chain_feedbacks_valid_count=fb_valid,
    )


# ---------------------------------------------------------------------------
# Intermediate
# ---------------------------------------------------------------------------


def render_intermediate_wallet_transaction_reason(
    ctx: dict[str, Any], score: Any
) -> dict[str, Any]:
    wallet = _mapping(ctx, "wallet_transaction_data")
    nonce_1m = _int_metric(
        wallet.get("nonce_delta_1month") if wallet else ctx.get("nonce_delta_1month")
    )

    if nonce_1m >= 900:
        expected: Decimal | int | float = Decimal("1.0")
    elif nonce_1m >= 450:
        expected = Decimal("0.5")
    elif nonce_1m >= 120:
        expected = Decimal("0.25")
    else:
        expected = 0
    _verify_score("intermediate_wallet_transaction", score, expected)

    if score_eq(score, Decimal("1.0")):
        eng = (
            "Very strong wallet transaction volume over 30 days, indicating high "
            "real-world usage and operational health."
        )
        esp = (
            "Volumen de transacciones en wallet muy fuerte en 30 días, indicando "
            "alto uso real y salud operativa."
        )
    elif score_eq(score, Decimal("0.5")):
        eng = (
            "Good wallet transaction volume over 30 days, showing solid and "
            "consistent activity."
        )
        esp = (
            "Buen volumen de transacciones en wallet en 30 días, mostrando "
            "actividad sólida y consistente."
        )
    elif score_eq(score, Decimal("0.25")):
        eng = (
            "Moderate wallet transaction volume over 30 days, acceptable for "
            "growing agents."
        )
        esp = (
            "Volumen de transacciones moderado en 30 días, aceptable para agentes "
            "en crecimiento."
        )
    else:
        eng = (
            "No meaningful wallet transaction volume detected over 30 days, "
            "raising concerns about operational engagement."
        )
        esp = (
            "No se detecta volumen significativo de transacciones en 30 días, "
            "generando preocupación sobre el engagement operativo."
        )

    return build_reason(
        eng,
        esp,
        nonce_field_used="nonce_delta_1month",
        value=nonce_1m,
        data_window_days=30,
        strict_mode=True,
    )


def render_intermediate_external_audits_reason(
    ctx: dict[str, Any], score: Any
) -> dict[str, Any]:
    audits = _mapping(ctx, "external_audits_summary")
    audit_count = _int_metric(audits.get("valid_count") if audits else None)
    avg_score = mapping_get_num(audits, "avg_score", 0) if audits else to_decimal(0)

    expected: Decimal | int = 0
    if audit_count > 0:
        if avg_score >= 70:
            expected = Decimal("3.0")
        elif avg_score >= 50:
            expected = Decimal("2.0")
        elif avg_score >= 0:
            expected = Decimal("1.0")
    _verify_score("intermediate_external_audits", score, expected)

    if score_eq(score, Decimal("3.0")):
        eng = (
            "Strong external audit presence with high average quality (≥70%), "
            "providing excellent third-party validation."
        )
        esp = (
            "Fuerte presencia de auditorías externas con alta calidad promedio "
            "(≥70%), brindando excelente validación de terceros."
        )
    elif score_eq(score, Decimal("2.0")):
        eng = "Moderate external audit presence with acceptable quality."
        esp = "Presencia moderada de auditorías externas con calidad aceptable."
    elif score_eq(score, Decimal("1.0")):
        eng = (
            "Basic external audit presence, offering limited but positive "
            "validation."
        )
        esp = (
            "Presencia básica de auditorías externas, ofreciendo validación "
            "limitada pero positiva."
        )
    else:
        eng = (
            "No meaningful external audits found, missing important third-party "
            "credibility signals."
        )
        esp = (
            "No se encontraron auditorías externas significativas, faltando "
            "señales importantes de credibilidad de terceros."
        )

    return build_reason(
        eng,
        esp,
        valid_audits_count=audit_count,
        avg_score=sql_round(avg_score, 2),
    )


def render_intermediate_protocol_activities_reason(
    ctx: dict[str, Any], score: Any
) -> dict[str, Any]:
    prot = _mapping(ctx, "protocol_activity_summary")
    prot_count = _int_metric(prot.get("valid_count") if prot else None)
    avg_prot = mapping_get_num(prot, "avg_score", 0) if prot else to_decimal(0)

    expected: Decimal | int = 0
    if 1 <= prot_count <= 7 and prot_count > 0:
        if avg_prot >= 60:
            expected = Decimal("2.5")
        elif avg_prot >= 40:
            expected = Decimal("1.5")
        elif avg_prot >= 0:
            expected = Decimal("0.75")
    _verify_score("intermediate_protocol_activities", score, expected)

    if score_eq(score, Decimal("2.5")):
        eng = (
            "Strong protocol activity with good volume and quality, demonstrating "
            "real on-chain engagement."
        )
        esp = (
            "Fuerte actividad en protocolos con buen volumen y calidad, "
            "demostrando engagement real on-chain."
        )
    elif score_eq(score, Decimal("1.5")):
        eng = "Moderate protocol activity, showing acceptable operational usage."
        esp = "Actividad moderada en protocolos, mostrando uso operativo aceptable."
    elif score_eq(score, Decimal("0.75")):
        eng = "Basic protocol activity, providing minimal but positive signals."
        esp = (
            "Actividad básica en protocolos, proporcionando señales mínimas pero "
            "positivas."
        )
    else:
        eng = (
            "No meaningful protocol activity detected, limiting demonstrated "
            "real-world usage."
        )
        esp = (
            "No se detecta actividad significativa en protocolos, limitando la "
            "demostración de uso real."
        )

    return build_reason(
        eng,
        esp,
        valid_activities_count=prot_count,
        avg_score=sql_round(avg_prot, 2),
    )


def render_multichain_presence_quality_reason(
    ctx: dict[str, Any], score: Any
) -> dict[str, Any]:
    mc = _mapping(ctx, "multichain_data")
    high_value = _as_bool(
        mc.get("has_high_value_chain_presence")
        if mc
        else ctx.get("has_high_value_chain_presence")
    )
    weighted = (
        mapping_get_num(mc, "weighted_multichain_activity_score", 0)
        if mc
        else to_decimal(ctx.get("weighted_multichain_activity_score"), 0)
    )
    shallow = _as_bool(
        mc.get("shallow_multichain_spread") if mc else ctx.get("shallow_multichain_spread")
    )
    active_chains = _int_metric(
        mc.get("active_chains_count") if mc else ctx.get("active_chains_count")
    )

    if high_value and weighted >= Decimal("1.5"):
        expected: Decimal | int = Decimal("2.5")
    elif high_value:
        expected = Decimal("1.5")
    elif shallow:
        expected = Decimal("-1.0")
    elif active_chains >= 3 and weighted < Decimal("1.0"):
        expected = Decimal("-0.5")
    else:
        expected = 0
    _verify_score("multichain_presence_quality", score, expected)

    if score_eq(score, Decimal("2.5")):
        eng = "Excellent multichain presence with strong activity in high-value chains."
        esp = "Excelente presencia multichain con actividad fuerte en chains de alto valor."
    elif score_eq(score, Decimal("1.5")):
        eng = "Good presence in at least one high-value chain."
        esp = "Buena presencia en al menos una chain de alto valor."
    elif score_eq(score, Decimal("-1.0")):
        eng = "Superficial activity spread across too many low-value chains."
        esp = "Actividad superficial dispersa en demasiadas chains de bajo valor."
    elif score_eq(score, Decimal("-0.5")):
        eng = "Activity spread across multiple chains without meaningful quality."
        esp = "Actividad dispersa en múltiples chains sin calidad significativa."
    else:
        eng = "No significant multichain presence quality signal detected."
        esp = "No se detecta señal significativa de calidad de presencia multichain."

    return build_reason(
        eng,
        esp,
        has_high_value_chain_presence=high_value,
        weighted_multichain_activity_score=sql_round(weighted, 4),
        shallow_multichain_spread=shallow,
        active_chains_count=active_chains,
    )


# ---------------------------------------------------------------------------
# Advanced
# ---------------------------------------------------------------------------


def render_advanced_external_audits_reason(
    ctx: dict[str, Any], score: Any
) -> dict[str, Any]:
    audits = _mapping(ctx, "external_audits_summary")
    audit_count = _int_metric(audits.get("valid_count") if audits else None)
    avg_score = mapping_get_num(audits, "avg_score", 0) if audits else to_decimal(0)

    expected: Decimal | int = 0
    if audit_count >= 2 and audit_count > 0:
        if avg_score >= 90:
            expected = Decimal("3.0")
        elif avg_score >= 80:
            expected = Decimal("2.5")
        elif avg_score >= 70:
            expected = Decimal("2.0")
        elif avg_score >= 60:
            expected = Decimal("1.0")
    _verify_score("advanced_external_audits", score, expected)

    if score_eq(score, Decimal("3.0")):
        eng = (
            "Exceptional advanced external audit coverage with very high quality "
            "(≥90%), representing best-in-class third-party validation."
        )
        esp = (
            "Cobertura excepcional de auditorías externas avanzadas con calidad "
            "muy alta (≥90%), representando validación de terceros de clase mundial."
        )
    elif score_ge(score, Decimal("2.5")):
        eng = "Strong advanced external audit coverage with excellent quality."
        esp = (
            "Fuerte cobertura de auditorías externas avanzadas con calidad "
            "excelente."
        )
    elif score_ge(score, Decimal("2.0")):
        eng = "Good advanced external audit coverage with solid quality."
        esp = "Buena cobertura de auditorías externas avanzadas con calidad sólida."
    else:
        eng = (
            "Limited advanced external audit coverage or quality, reducing "
            "high-end credibility signals."
        )
        esp = (
            "Cobertura o calidad limitada de auditorías externas avanzadas, "
            "reduciendo señales de credibilidad de alto nivel."
        )

    return build_reason(
        eng,
        esp,
        valid_audits_count=audit_count,
        avg_score=sql_round(avg_score, 2),
    )


def render_advanced_protocol_activities_reason(
    ctx: dict[str, Any], score: Any
) -> dict[str, Any]:
    prot = _mapping(ctx, "protocol_activity_summary")
    prot_count = _int_metric(prot.get("valid_count") if prot else None)
    avg_prot = mapping_get_num(prot, "avg_score", 0) if prot else to_decimal(0)

    expected: Decimal | int = 0
    if prot_count >= 10 and prot_count > 0:
        if avg_prot >= 90:
            expected = Decimal("1.0")
        elif avg_prot >= 80:
            expected = Decimal("0.75")
        elif avg_prot >= 70:
            expected = Decimal("0.5")
    _verify_score("advanced_protocol_activities", score, expected)

    if score_eq(score, Decimal("1.0")):
        eng = (
            "Exceptional advanced protocol activity with very high quality and "
            "volume, reflecting elite operational maturity."
        )
        esp = (
            "Actividad excepcional en protocolos avanzados con calidad y volumen "
            "muy altos, reflejando madurez operativa de élite."
        )
    elif score_ge(score, Decimal("0.75")):
        eng = "Strong advanced protocol activity with good quality."
        esp = "Fuerte actividad en protocolos avanzados con buena calidad."
    elif score_ge(score, Decimal("0.5")):
        eng = "Moderate advanced protocol activity."
        esp = "Actividad moderada en protocolos avanzados."
    else:
        eng = (
            "Limited advanced protocol activity, reducing evidence of high-end "
            "on-chain engagement."
        )
        esp = (
            "Actividad avanzada en protocolos limitada, reduciendo la evidencia de "
            "engagement on-chain de alto nivel."
        )

    return build_reason(
        eng,
        esp,
        valid_activities_count=prot_count,
        avg_score=sql_round(avg_prot, 2),
    )


def render_identity_analysis_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    ident = as_mapping(ctx.get("identity_analysis"))
    has_score = (
        ident is not None
        and len(ident) > 0
        and ident.get("identity_score") is not None
    )

    if has_score:
        assert ident is not None
        raw_identity = to_decimal(ident.get("identity_score"), 0)
        expected = min(Decimal("2.0"), raw_identity / Decimal(50))
        _verify_score("identity_analysis", score, expected)

        if score_eq(score, Decimal("2.0")):
            eng = (
                "Excellent identity analysis with strong soulbound verification, "
                "providing maximum trust and authenticity signals."
            )
            esp = (
                "Análisis de identidad excelente con fuerte verificación soulbound, "
                "proporcionando señales máximas de confianza y autenticidad."
            )
        elif score_ge(score, Decimal("1.0")):
            eng = (
                "Good identity analysis, contributing positively to agent "
                "credibility."
            )
            esp = (
                "Buen análisis de identidad, contribuyendo positivamente a la "
                "credibilidad del agente."
            )
        else:
            eng = "Limited identity analysis, reducing overall trust signals."
            esp = (
                "Análisis de identidad limitado, reduciendo las señales generales "
                "de confianza."
            )

        return build_reason(
            eng,
            esp,
            identity_score=whole_number(raw_identity),
            identity_stage=ident.get("identity_stage"),
        )

    _verify_score("identity_analysis", score, 0)
    return build_reason(
        "No identity analysis present, missing critical authenticity and trust "
        "validation.",
        "No hay análisis de identidad presente, faltando validación crítica de "
        "autenticidad y confianza.",
    )


# ---------------------------------------------------------------------------
# Penalty
# ---------------------------------------------------------------------------


def _warning_message_esp(warn: dict[str, Any], msg_eng: Any, warn_type: Any) -> Any:
    details = warn.get("details")
    details_map = as_mapping(details) if details is not None else {}
    if details_map is None:
        details_map = {}

    if warn_type == "duplication_metadata":
        return "Agente con metadatos duplicados"
    if warn_type == "multi_agent_wallet":
        count = coalesce(details_map.get("shared_with_agents_count"), "0")
        return f"Wallet compartida con {count} agente(s)"
    if warn_type == "dummy_metadata":
        return "Metadatos de baja calidad"
    if warn_type == "attestations_spam":
        count = coalesce(details_map.get("spam_count"), "0")
        return f"Attestations spam detectadas ({count})"
    if warn_type == "external_audit_warning":
        return "Auditoría externa con señales de riesgo"
    if warn_type == "high_revocations":
        return coalesce(msg_eng, "Alta tasa de revocaciones")
    if warn_type == "owner_inactive_agents":
        return "Owner con alta proporción de agentes inactivos"
    if warn_type == "high_ownership_churn":
        return "Alta rotación reciente de ownership"
    if warn_type == "transactional_wallet_same_as_owner":
        return "Wallet transaccional coincide con wallet del owner"
    if warn_type == "lower_realness":
        return coalesce(msg_eng, "Puntaje Realness bajo")
    if warn_type == "lower_metadata_richness":
        return coalesce(msg_eng, "Riqueza de metadatos baja")
    return coalesce(msg_eng, warn_type)


def render_penalty_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    warnings_raw = _as_list(ctx.get("warnings_summary"))
    penalty = Decimal(0)
    warnings_list: list[dict[str, Any]] = []

    for elem in warnings_raw:
        warn = as_mapping(elem) or {}
        impact = to_decimal(warn.get("score_impact"), 0)
        penalty += impact
        msg_eng = warn.get("message")
        warn_type = warn.get("type")
        details = warn.get("details")
        details_obj = as_mapping(details) if details is not None else {}
        if details_obj is None:
            details_obj = {}

        warnings_list.append(
            {
                "type": warn_type,
                "severity": warn.get("severity"),
                "score_impact": whole_number(impact),
                "message_eng": msg_eng,
                "message_esp": _warning_message_esp(warn, msg_eng, warn_type),
                "details": details_obj,
            }
        )

    _verify_score("penalty", score, penalty)

    # SQL CASE usa v_penalty_score (computado de warnings), no un score externo.
    if score_lt(penalty, 0):
        eng = (
            "Active warnings detected; cumulative score impact reduces overall "
            "Measure pillar score."
        )
        esp = (
            "Advertencias activas detectadas; el impacto acumulado reduce el "
            "puntaje del pilar Measure."
        )
    else:
        eng = "No active warnings; no penalty applied in this calculation cycle."
        esp = (
            "Sin advertencias activas; no se aplica penalización en este ciclo "
            "de cálculo."
        )

    # Orden como jsonb_build_object SQL: métricas primero, luego reasons.
    return {
        "total_penalty": whole_number(penalty),
        "warnings": warnings_list,
        "warnings_count": len(warnings_list),
        "reason_eng": eng,
        "reason_esp": esp,
    }


# ---------------------------------------------------------------------------
# Pillar summary
# ---------------------------------------------------------------------------


def render_measure_pillar_summary(
    ctx: dict[str, Any], scores: dict[str, Any]
) -> dict[str, Any]:
    total = _pillar_total(scores)
    penalty = to_decimal(scores.get("penalty_score"), 0)
    metadata = to_decimal(scores.get("metadata_richness_score"), 0)
    existence = to_decimal(scores.get("existence_score"), 0)
    wallet = to_decimal(scores.get("intermediate_wallet_transaction_score"), 0)
    multichain = to_decimal(scores.get("multichain_presence_quality_score"), 0)
    identity = to_decimal(scores.get("identity_analysis_score"), 0)

    if score_ge(total, Decimal("22")):
        overall_eng = (
            "The agent demonstrates excellent foundational maturity and strong "
            "market credibility."
        )
        overall_esp = (
            "El agente demuestra una excelente madurez fundamental y fuerte "
            "credibilidad en el mercado."
        )
    elif score_ge(total, Decimal("18.5")):
        overall_eng = (
            "The agent has solid foundations with good legitimacy signals, though "
            "some strengthening is still recommended."
        )
        overall_esp = (
            "El agente tiene fundamentos sólidos con buenas señales de "
            "legitimidad, aunque se recomienda fortalecer algunos aspectos."
        )
    elif score_ge(total, Decimal("15")):
        overall_eng = (
            "The agent has acceptable foundations but shows clear areas of "
            "weakness that could limit its perceived trustworthiness."
        )
        overall_esp = (
            "El agente tiene fundamentos aceptables pero presenta claras "
            "debilidades que podrían limitar su confianza percibida."
        )
    elif score_ge(total, Decimal("11")):
        overall_eng = (
            "The agent has weak foundational quality. Multiple red flags are "
            "affecting its credibility in the ecosystem."
        )
        overall_esp = (
            "El agente tiene una calidad fundamental débil. Múltiples alertas "
            "rojas afectan su credibilidad en el ecosistema."
        )
    else:
        overall_eng = (
            "The agent exhibits very weak foundational quality. Significant "
            "concerns exist regarding its legitimacy and professionalism."
        )
        overall_esp = (
            "El agente presenta una calidad fundamental muy débil. Existen "
            "preocupaciones importantes sobre su legitimidad y profesionalismo."
        )

    if score_lt(penalty, 0):
        mid_eng = "Active warnings are materially undermining trust and score. "
        mid_esp = (
            "Advertencias activas están socavando materialmente la confianza "
            "y el puntaje. "
        )
    elif score_ge(multichain, Decimal("2")):
        mid_eng = (
            " Strong multichain quality significantly strengthens operational "
            "credibility. "
        )
        mid_esp = (
            " Una fuerte calidad multichain refuerza significativamente la "
            "credibilidad operativa. "
        )
    elif score_lt(multichain, 0):
        mid_eng = (
            " Weak or superficial multichain presence reduces perceived focus "
            "and professionalism. "
        )
        mid_esp = (
            " Una presencia multichain débil o superficial reduce el foco y "
            "profesionalismo percibidos. "
        )
    elif score_lt(metadata, Decimal("2")):
        mid_eng = (
            "Poor metadata quality significantly weakens the agent's "
            "professional appearance. "
        )
        mid_esp = (
            "La baja calidad de metadatos debilita significativamente la "
            "apariencia profesional del agente. "
        )
    elif score_lt(existence, Decimal("4")):
        mid_eng = (
            "Lack of sufficient on-chain proof of existence raises serious "
            "legitimacy concerns. "
        )
        mid_esp = (
            "La falta de prueba suficiente de existencia on-chain genera serias "
            "dudas sobre su legitimidad. "
        )
    else:
        mid_eng = "The agent shows reasonable foundational elements. "
        mid_esp = "El agente muestra elementos fundamentales razonables. "

    if score_lt(total, Decimal("11")):
        tail_eng = "high risk for partners and users."
        tail_esp = "alto riesgo para socios y usuarios."
    elif score_lt(total, Decimal("15")):
        tail_eng = "moderate to high risk."
        tail_esp = "riesgo moderado a alto."
    elif score_lt(total, Decimal("18.5")):
        tail_eng = "moderate risk."
        tail_esp = "riesgo moderado."
    else:
        tail_eng = "relatively low risk."
        tail_esp = "riesgo relativamente bajo."

    business_eng = (
        "This Measure pillar evaluates the agent's core identity strength, "
        "proof of existence, and foundational credibility. "
        + mid_eng
        + "Overall, the current level suggests "
        + tail_eng
    )
    business_esp = (
        "Este pilar Measure evalúa la fortaleza de la identidad del agente, "
        "la prueba de existencia real y su credibilidad fundamental. "
        + mid_esp
        + "En general, el nivel actual sugiere "
        + tail_esp
    )

    strengths_eng: list[str] = []
    strengths_esp: list[str] = []
    if score_ge(existence, Decimal("5")):
        strengths_eng.append("Demonstrated real on-chain presence and activity")
        strengths_esp.append("Presencia real demostrada mediante actividad on-chain")
    if score_ge(metadata, Decimal("3")):
        strengths_eng.append("Professional and complete identity presentation")
        strengths_esp.append("Presentación profesional y completa de identidad")
    if score_eq(wallet, Decimal("1")):
        strengths_eng.append("Strong operational activity level")
        strengths_esp.append("Fuerte nivel de actividad operativa")
    if score_ge(multichain, Decimal("2")):
        strengths_eng.append(
            "High-quality multichain presence across valuable chains"
        )
        strengths_esp.append(
            "Presencia multichain de alta calidad en chains valiosas"
        )
    if score_ge(identity, Decimal("2")):
        strengths_eng.append(
            "Strong identity verification and authenticity signals"
        )
        strengths_esp.append(
            "Fuertes señales de verificación de identidad y autenticidad"
        )

    concerns_eng: list[str] = []
    concerns_esp: list[str] = []
    if score_lt(penalty, 0):
        concerns_eng.append(
            "Active warnings detected — review severity and resolve operational risks"
        )
        concerns_esp.append(
            "Advertencias activas detectadas — revisar severidad y resolver "
            "riesgos operativos"
        )
    if score_lt(metadata, Decimal("2.8")):
        concerns_eng.append(
            "Insufficient metadata quality affecting professional perception"
        )
        concerns_esp.append(
            "Calidad insuficiente de metadatos afectando la percepción profesional"
        )
    if score_lt(existence, Decimal("4")):
        concerns_eng.append("Weak proof of real existence and operational history")
        concerns_esp.append("Débil prueba de existencia real e historial operativo")
    if score_lt(multichain, 0):
        concerns_eng.append(
            "Superficial or low-quality multichain spread detected"
        )
        concerns_esp.append(
            "Dispersión multichain superficial o de baja calidad detectada"
        )

    if score_lt(penalty, 0):
        rec_eng = (
            "Priority: Address active warnings (duplication, wallet sharing, spam, "
            "etc.) as they are materially damaging credibility and score."
        )
        rec_esp = (
            "Prioridad: Atender advertencias activas (duplicación, wallet "
            "compartida, spam, etc.) ya que dañan materialmente la credibilidad "
            "y el puntaje."
        )
    elif score_lt(metadata, Decimal("2")):
        rec_eng = (
            "Focus on improving metadata completeness (name, description, image "
            "and tags) to strengthen professional perception."
        )
        rec_esp = (
            "Enfocarse en mejorar la completitud de los metadatos (nombre, "
            "descripción, imagen y etiquetas) para fortalecer la percepción "
            "profesional."
        )
    elif score_lt(existence, Decimal("4")):
        rec_eng = (
            "Generate consistent on-chain activity and wallet usage to provide "
            "clear proof of real existence."
        )
        rec_esp = (
            "Generar actividad on-chain consistente y uso de wallet para "
            "proporcionar prueba clara de existencia real."
        )
    elif score_lt(multichain, 0):
        rec_eng = (
            "Focus activity on 1-2 high-value chains instead of spreading across "
            "many low-value ones."
        )
        rec_esp = (
            "Concentrar la actividad en 1-2 chains de alto valor en lugar de "
            "dispersarla en muchas de bajo valor."
        )
    else:
        rec_eng = (
            "Continue strengthening metadata quality and on-chain presence to "
            "reach higher maturity levels."
        )
        rec_esp = (
            "Continuar fortaleciendo la calidad de metadatos y presencia on-chain "
            "para alcanzar niveles superiores de madurez."
        )

    return {
        "overall_assessment_eng": overall_eng,
        "overall_assessment_esp": overall_esp,
        "business_interpretation_eng": business_eng,
        "business_interpretation_esp": business_esp,
        "key_strengths_eng": jsonb_agg(strengths_eng),
        "key_strengths_esp": jsonb_agg(strengths_esp),
        "main_concerns_eng": jsonb_agg(concerns_eng),
        "main_concerns_esp": jsonb_agg(concerns_esp),
        "recommendation_eng": rec_eng,
        "recommendation_esp": rec_esp,
    }


_ITEM_RENDERERS = {
    "metadata_richness_reason": render_metadata_richness_reason,
    "existence_reason": render_existence_reason,
    "intermediate_wallet_transaction_reason": render_intermediate_wallet_transaction_reason,
    "intermediate_external_audits_reason": render_intermediate_external_audits_reason,
    "intermediate_protocol_activities_reason": render_intermediate_protocol_activities_reason,
    "multichain_presence_quality_reason": render_multichain_presence_quality_reason,
    "advanced_external_audits_reason": render_advanced_external_audits_reason,
    "identity_analysis_reason": render_identity_analysis_reason,
    "advanced_protocol_activities_reason": render_advanced_protocol_activities_reason,
}


def render_measure_reasons(ctx: dict[str, Any], scores: dict[str, Any]) -> dict[str, Any]:
    """Devuelve {reason_column: reason_dict, ..., penalty_reason, pillar_summary}.

    Sin `last_calculated` en pillar_summary (paridad Stage 2).
    """
    out: dict[str, Any] = {}
    for item in (*MEASURES.basic, *MEASURES.intermediate, *MEASURES.advanced):
        renderer = _ITEM_RENDERERS[item.reason_column]
        out[item.reason_column] = renderer(ctx, scores.get(item.score_column))
    out["penalty_reason"] = render_penalty_reason(ctx, scores.get("penalty_score"))
    out["pillar_summary"] = render_measure_pillar_summary(ctx, scores)
    return out


def _pillar_total(scores: dict[str, Any]) -> Decimal:
    if scores.get("pillar_score") is not None:
        return to_decimal(scores["pillar_score"])
    blocks = (
        scores.get("block_basic_score"),
        scores.get("block_intermediate_score"),
        scores.get("block_advanced_score"),
    )
    if all(b is not None for b in blocks):
        total = sum((to_decimal(b) for b in blocks), Decimal(0))
        total += to_decimal(scores.get("penalty_score"), 0)
        return max(total, Decimal(0))
    total = Decimal(0)
    for item in (*MEASURES.basic, *MEASURES.intermediate, *MEASURES.advanced):
        total += to_decimal(scores.get(item.score_column), 0)
    total += to_decimal(scores.get("penalty_score"), 0)
    return max(total, Decimal(0))


def _verify_score(label: str, passed: Any, expected: Any) -> None:
    if passed is None:
        return
    if not score_eq(passed, expected):
        logger.debug(
            "measure score mismatch item=%s passed=%s expected=%s (emitting from inputs)",
            label,
            passed,
            expected,
        )
