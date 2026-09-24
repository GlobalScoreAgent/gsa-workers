"""Render de reasons + pillar_summary del pilar History (paridad SQL).

Fuente canónica: index_humi.agent_pillar_history_calculate
(migración 00000000000060_job_control_daily_index_pipeline.sql).

No recalcula scores para decidir el texto cuando SQL hace CASE WHEN score = X:
usa el score persistido en `scores`. El control flow de IFs sobre inputs replica SQL.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from pillar_spec import HISTORY

from .util import (
    add_calendar_months,
    agent_age_months,
    as_date,
    as_mapping,
    as_of_date,
    build_reason,
    coalesce,
    days_since,
    jsonb_agg,
    mapping_get_num,
    score_eq,
    score_ge,
    score_gt,
    score_lt,
    sql_round,
    to_decimal,
    whole_number,
)

logger = logging.getLogger("humi_reason_publisher.render.history")


def render_owner_wallet_active_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    has_active = bool(coalesce(ctx.get("owner_has_active_wallet_in_chain_info"), False))
    active_agents = to_decimal(ctx.get("owner_portafolio_agent_active_total"), 0)

    if has_active or active_agents > 0:
        _verify_score("owner_wallet_active", score, 5)
        return build_reason(
            "Owner wallet is confirmed active or supports active agents in portfolio, "
            "demonstrating real participation and operational legitimacy.",
            "La wallet del owner está confirmada como activa o soporta agentes activos "
            "en el portafolio, demostrando participación real y legitimidad operativa.",
            has_active_wallet_in_chain_info=has_active,
            active_agents_in_portfolio=whole_number(active_agents),
        )

    _verify_score("owner_wallet_active", score, 0)
    return build_reason(
        "No active wallet or active agents in portfolio detected, indicating potential "
        "low owner engagement or inactivity risk.",
        "No se detecta wallet activa ni agentes activos en el portafolio, indicando "
        "posible bajo engagement del owner o riesgo de inactividad.",
    )


def render_agent_owner_stability_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    as_of = as_of_date(ctx)
    age_months = agent_age_months(ctx.get("on_chain_created_at"), as_of)
    owner_changes_raw = ctx.get("owner_changes")
    owner_changes_metric = int(to_decimal(coalesce(owner_changes_raw, 0)))

    if age_months < 6:
        # Score IF uses COALESCE(owner_changes, 1) = 1; metric uses COALESCE(..., 0).
        expected = 5 if to_decimal(coalesce(owner_changes_raw, 1)) == 1 else 0
        _verify_score("agent_owner_stability", score, expected)
        if score_eq(score, 5):
            eng = (
                "Owner stability is excellent for a young agent (<6 months) with no "
                "ownership changes, showing strong commitment."
            )
            esp = (
                "La estabilidad del owner es excelente para un agente joven (<6 meses) "
                "sin cambios de propiedad, mostrando fuerte compromiso."
            )
        else:
            eng = (
                "Owner changes detected in a young agent, increasing perceived "
                "instability risk."
            )
            esp = (
                "Se detectaron cambios de owner en un agente joven, aumentando el "
                "riesgo percibido de inestabilidad."
            )
    else:
        expected = 5 if to_decimal(coalesce(owner_changes_raw, 0)) <= 2 else 0
        _verify_score("agent_owner_stability", score, expected)
        if score_eq(score, 5):
            eng = (
                "Excellent ownership stability for a mature agent (≥6 months) with "
                "minimal changes, reflecting high trust and continuity."
            )
            esp = (
                "Excelente estabilidad de propiedad para un agente maduro (≥6 meses) "
                "con cambios mínimos, reflejando alta confianza y continuidad."
            )
        else:
            eng = (
                "Multiple ownership changes detected in a mature agent, raising "
                "concerns about stability and long-term commitment."
            )
            esp = (
                "Múltiples cambios de propiedad detectados en un agente maduro, "
                "generando preocupación sobre estabilidad y compromiso a largo plazo."
            )

    return build_reason(
        eng,
        esp,
        agent_age_months=age_months,
        owner_changes=owner_changes_metric,
    )


def render_portafolio_agent_active_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    total_agents = to_decimal(ctx.get("owner_portafolio_agent_total"), 0)
    active_agents = to_decimal(ctx.get("owner_portafolio_agent_active_total"), 0)

    if total_agents > 0:
        active_pct = (active_agents / total_agents) * 100
        pct = sql_round(active_pct, 2)
        if active_pct >= 80:
            _verify_score("portafolio_agent_active", score, 3)
            return build_reason(
                "Excellent portfolio health with ≥80% active agents, demonstrating "
                "strong owner management and operational consistency.",
                "Salud del portafolio excelente con ≥80% de agentes activos, "
                "demostrando fuerte gestión del owner y consistencia operativa.",
                percentage_active=pct,
            )
        if active_pct >= 50:
            _verify_score("portafolio_agent_active", score, Decimal("1.5"))
            return build_reason(
                "Moderate portfolio health (50–79% active agents), indicating "
                "acceptable but improvable owner engagement.",
                "Salud del portafolio moderada (50–79% agentes activos), indicando "
                "engagement del owner aceptable pero mejorable.",
                percentage_active=pct,
            )
        _verify_score("portafolio_agent_active", score, 0)
        return build_reason(
            "Low portfolio health (<50% active agents), signaling potential owner "
            "inactivity or portfolio neglect risk.",
            "Baja salud del portafolio (<50% agentes activos), señalando posible "
            "inactividad del owner o riesgo de abandono del portafolio.",
            percentage_active=pct,
        )

    _verify_score("portafolio_agent_active", score, 0)
    return build_reason(
        "No agents in portfolio, making active portfolio analysis not applicable.",
        "No hay agentes en el portafolio, por lo que el análisis de portafolio "
        "activo no aplica.",
    )


def render_portafolio_basic_external_audits_reason(
    ctx: dict[str, Any], score: Any
) -> dict[str, Any]:
    audits = as_mapping(ctx.get("owner_portafolio_agent_external_audits")) or {}
    total_audited = int(mapping_get_num(audits, "total_valid_audits", 0))

    if total_audited >= 1:
        _verify_score("portafolio_basic_external_audits", score, Decimal("1.5"))
        return build_reason(
            "At least one external audit present in the owner’s portfolio, "
            "providing strong external validation and credibility.",
            "Al menos una auditoría externa presente en el portafolio del owner, "
            "proporcionando fuerte validación y credibilidad externa.",
            total_audited_agents=total_audited,
        )

    _verify_score("portafolio_basic_external_audits", score, 0)
    return build_reason(
        "No external audits found in the portfolio, missing an important layer of "
        "third-party validation.",
        "No se encontraron auditorías externas en el portafolio, faltando una capa "
        "importante de validación de terceros.",
        total_audited_agents=0,
    )


def render_portafolio_warnings_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    total_agents = to_decimal(ctx.get("owner_portafolio_agent_total"), 0)
    warnings = as_mapping(ctx.get("owner_portafolio_agent_warnings")) or {}
    total_warnings = mapping_get_num(warnings, "total_agents_with_warnings", 0)

    if total_agents > 0:
        warnings_pct = (total_warnings / total_agents) * 100
        pct = sql_round(warnings_pct, 2)
        if warnings_pct <= 10:
            _verify_score("portafolio_warnings", score, Decimal("1.5"))
            return build_reason(
                "Clean portfolio with very low warnings (≤10%), indicating excellent "
                "owner hygiene and low risk profile.",
                "Portafolio limpio con muy pocas advertencias (≤10%), indicando "
                "excelente higiene del owner y bajo perfil de riesgo.",
                warnings_pct=pct,
            )
        _verify_score("portafolio_warnings", score, 0)
        return build_reason(
            "Elevated warnings in portfolio (>10%), increasing perceived risk and "
            "potential compliance concerns.",
            "Advertencias elevadas en el portafolio (>10%), aumentando el riesgo "
            "percibido y posibles preocupaciones de cumplimiento.",
            warnings_pct=pct,
        )

    _verify_score("portafolio_warnings", score, 0)
    return build_reason(
        "No agents in portfolio for warnings analysis.",
        "No hay agentes en el portafolio para análisis de advertencias.",
    )


def render_owner_antiquity_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    as_of = as_of_date(ctx)
    wallet_created = ctx.get("wallet_created_at")

    if wallet_created is None or as_date(wallet_created) is None:
        _verify_score("owner_antiquity", score, 0)
        return build_reason(
            "No first transaction date available, preventing antiquity assessment.",
            "No hay fecha de primera transacción disponible, imposibilitando la "
            "evaluación de antigüedad.",
        )

    created = as_date(wallet_created)
    assert created is not None
    if created >= add_calendar_months(as_of, -6):
        expected: Decimal | int = 1
    elif created >= add_calendar_months(as_of, -12):
        expected = 2
    else:
        expected = 3
    _verify_score("owner_antiquity", score, expected)

    if score_eq(score, 3):
        eng = (
            "Owner demonstrates strong antiquity (>2 years), reflecting deep "
            "ecosystem experience and high credibility."
        )
        esp = (
            "El owner demuestra fuerte antigüedad (>2 años), reflejando profunda "
            "experiencia en el ecosistema y alta credibilidad."
        )
    elif score_eq(score, 2):
        eng = (
            "Owner shows good antiquity (1-2 years), indicating solid experience "
            "and moderate maturity."
        )
        esp = (
            "El owner muestra buena antigüedad (1-2 años), indicando experiencia "
            "sólida y madurez moderada."
        )
    else:
        eng = (
            "Owner is relatively new (<1 year), limiting perceived long-term "
            "commitment."
        )
        esp = (
            "El owner es relativamente nuevo (<1 año), limitando el compromiso "
            "percibido a largo plazo."
        )

    return build_reason(
        eng,
        esp,
        days_since_first_tx=days_since(wallet_created, as_of),
    )


def render_portafolio_quality_agent_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    total_agents = to_decimal(ctx.get("owner_portafolio_agent_total"), 0)
    richness = as_mapping(ctx.get("owner_portafolio_agent_metadata_richness"))
    services = as_mapping(ctx.get("owner_portafolio_agents_metadata_services"))

    metadata_richness_avg = to_decimal(0)
    metadata_score = to_decimal(0)
    if richness is not None and total_agents > 0:
        good_count = mapping_get_num(richness, "Strong / Well-Developed", 0) + mapping_get_num(
            richness, "Excellent / Production-Ready", 0
        )
        metadata_pct = (good_count / total_agents) * 100
        metadata_richness_avg = metadata_pct
        metadata_score = min(to_decimal("0.75"), (metadata_pct / 100) * to_decimal("0.75"))

    services_with_at_least_one = to_decimal(0)
    services_pct = to_decimal(0)
    services_score = to_decimal(0)
    if services is not None:
        services_with_at_least_one = (
            mapping_get_num(services, "agents_with_one_service", 0)
            + mapping_get_num(services, "agents_with_two_services", 0)
            + mapping_get_num(services, "agents_with_three_services", 0)
            + mapping_get_num(services, "agents_with_four_services", 0)
            + mapping_get_num(services, "agents_with_five_or_more_services", 0)
            + mapping_get_num(services, "agents_with_specialized_services", 0)
        )
        if total_agents > 0:
            services_pct = (services_with_at_least_one / total_agents) * 100
            services_score = to_decimal("0.75") if services_pct >= 50 else to_decimal(0)

    _verify_score("portafolio_quality_agent", score, metadata_score + services_score)

    if score_eq(score, Decimal("1.5")):
        eng = (
            "Excellent combination of high metadata richness and widespread services "
            "across portfolio, reflecting professional technical quality."
        )
        esp = (
            "Excelente combinación de alta riqueza de metadatos y servicios extendidos "
            "en el portafolio, reflejando calidad técnica profesional."
        )
    elif score_gt(score, 0):
        eng = (
            "Moderate technical quality in portfolio with some metadata richness "
            "and services usage."
        )
        esp = (
            "Calidad técnica moderada en el portafolio con algo de riqueza de "
            "metadatos y uso de servicios."
        )
    else:
        eng = (
            "Limited technical quality in portfolio (low metadata richness and "
            "services adoption), reducing perceived sophistication."
        )
        esp = (
            "Calidad técnica limitada en el portafolio (baja riqueza de metadatos y "
            "adopción de servicios), reduciendo la sofisticación percibida."
        )

    return build_reason(
        eng,
        esp,
        metadata_avg_score=sql_round(metadata_richness_avg, 2),
        services_with_at_least_one=whole_number(services_with_at_least_one),
        services_pct=sql_round(services_pct, 2),
    )


def render_portafolio_advanced_external_audits_reason(
    ctx: dict[str, Any], score: Any
) -> dict[str, Any]:
    audits = as_mapping(ctx.get("owner_portafolio_agent_external_audits")) or {}
    active_agents = to_decimal(ctx.get("owner_portafolio_agent_active_total"), 0)
    total_audited = mapping_get_num(audits, "total_valid_audits", 0)
    good_excellent = mapping_get_num(audits, "agents_with_good_score", 0) + mapping_get_num(
        audits, "agents_with_excellent_score", 0
    )
    total_analyzed = total_audited

    coverage_pct = (
        (total_audited / active_agents) * 100 if active_agents > 0 else to_decimal(0)
    )
    quality_pct = (
        (good_excellent / total_analyzed) * 100 if total_analyzed > 0 else to_decimal(0)
    )

    cov = sql_round(coverage_pct, 2)
    qual = sql_round(quality_pct, 2)

    if coverage_pct >= 100 and quality_pct >= 70:
        _verify_score("portafolio_advanced_external_audits", score, 2)
        return build_reason(
            "100% audit coverage with high quality (≥70% good/excellent), representing "
            "best-in-class external validation across the entire portfolio.",
            "Cobertura de auditorías del 100% con alta calidad (≥70% bueno/excelente), "
            "representando validación externa de clase mundial en todo el portafolio.",
            coverage_pct=cov,
            quality_pct=qual,
        )
    if coverage_pct >= 50 and quality_pct >= 50:
        _verify_score("portafolio_advanced_external_audits", score, 1)
        return build_reason(
            "Moderate audit coverage (≥50%) with acceptable quality, providing solid "
            "but not elite external validation.",
            "Cobertura de auditorías moderada (≥50%) con calidad aceptable, "
            "proporcionando validación externa sólida pero no de élite.",
            coverage_pct=cov,
            quality_pct=qual,
        )

    _verify_score("portafolio_advanced_external_audits", score, 0)
    return build_reason(
        "Insufficient audit coverage or quality in portfolio, limiting external "
        "credibility and increasing perceived risk.",
        "Cobertura o calidad de auditorías insuficiente en el portafolio, limitando "
        "la credibilidad externa y aumentando el riesgo percibido.",
        coverage_pct=cov,
        quality_pct=qual,
    )


def render_portafolio_general_activity_reason(
    ctx: dict[str, Any], score: Any
) -> dict[str, Any] | None:
    """Si active_agents = 0, SQL no asigna reason (queda NULL)."""
    active_agents = to_decimal(ctx.get("owner_portafolio_agent_active_total"), 0)
    if active_agents <= 0:
        _verify_score("portafolio_general_activity", score, 0)
        return None

    attestations = as_mapping(ctx.get("owner_portafolio_agent_attestations")) or {}
    executions = as_mapping(ctx.get("owner_portafolio_agent_on_chain_executions")) or {}
    protocols = as_mapping(ctx.get("owner_portafolio_agent_activity_protocols")) or {}

    total_activity = (
        mapping_get_num(attestations, "total_valid_attestations", 0)
        + mapping_get_num(executions, "agents_with_executions", 0)
        + mapping_get_num(protocols, "total_protocol_activities", 0)
    )
    good_excellent_activity = mapping_get_num(
        attestations, "agents_with_good_score", 0
    ) + mapping_get_num(attestations, "agents_with_excellent_score", 0)

    activity_pct = (total_activity / active_agents) * 100
    quality_pct = (
        (good_excellent_activity / total_activity) * 100
        if total_activity > 0
        else to_decimal(0)
    )

    act = sql_round(activity_pct, 2)
    qual = sql_round(quality_pct, 2)

    if activity_pct >= 100 and quality_pct >= 70:
        _verify_score("portafolio_general_activity", score, Decimal("2.5"))
        return build_reason(
            "Exceptional portfolio-wide activity with full coverage and high quality, "
            "demonstrating strong sustained engagement.",
            "Actividad excepcional a nivel de portafolio con cobertura completa y "
            "alta calidad, demostrando fuerte engagement sostenido.",
            activity_pct=act,
            quality_pct=qual,
        )
    if activity_pct >= 50 and quality_pct >= 50:
        _verify_score("portafolio_general_activity", score, Decimal("1.25"))
        return build_reason(
            "Moderate portfolio activity with acceptable coverage and quality.",
            "Actividad moderada del portafolio con cobertura y calidad aceptables.",
            activity_pct=act,
            quality_pct=qual,
        )

    _verify_score("portafolio_general_activity", score, 0)
    return build_reason(
        "Low portfolio activity or poor quality, indicating limited owner engagement "
        "across the portfolio.",
        "Baja actividad del portafolio o baja calidad, indicando engagement limitado "
        "del owner en todo el portafolio.",
        activity_pct=act,
        quality_pct=qual,
    )


def render_history_pillar_summary(ctx: dict[str, Any], scores: dict[str, Any]) -> dict[str, Any]:
    """Replica v_pillar_summary; omite last_calculated a propósito (Stage 2)."""
    _ = ctx
    total = _pillar_total(scores)
    stability = scores.get("agent_owner_stability_score")
    warnings = scores.get("portafolio_warnings_score")
    quality = scores.get("portafolio_quality_agent_score")
    antiquity = scores.get("owner_antiquity_score")
    active_pf = scores.get("portafolio_agent_active_score")
    adv_audits = scores.get("portafolio_advanced_external_audits_score")

    if score_ge(total, 22):
        overall_eng = (
            "The agent has excellent historical maturity and strong ownership credibility."
        )
        overall_esp = (
            "El agente tiene una excelente madurez histórica y fuerte credibilidad de propiedad."
        )
    elif score_ge(total, Decimal("18.5")):
        overall_eng = (
            "The agent shows solid historical foundations with good portfolio health."
        )
        overall_esp = (
            "El agente muestra fundamentos históricos sólidos con buena salud de portafolio."
        )
    elif score_ge(total, 15):
        overall_eng = (
            "The agent has acceptable history but with noticeable weaknesses in "
            "stability or portfolio quality."
        )
        overall_esp = (
            "El agente tiene historia aceptable pero con debilidades notables en "
            "estabilidad o calidad de portafolio."
        )
    elif score_ge(total, 11):
        overall_eng = (
            "The agent has weak historical profile. Multiple concerns affect long-term trust."
        )
        overall_esp = (
            "El agente tiene un perfil histórico débil. Múltiples preocupaciones "
            "afectan la confianza a largo plazo."
        )
    else:
        overall_eng = (
            "The agent exhibits very weak historical maturity. Significant red flags "
            "in ownership and portfolio."
        )
        overall_esp = (
            "El agente presenta una madurez histórica muy débil. Existen alertas rojas "
            "importantes en propiedad y portafolio."
        )

    if score_eq(stability, 0):
        mid_eng = "Ownership instability stands out as a critical issue. "
        mid_esp = "La inestabilidad de propiedad destaca como un problema crítico. "
    elif score_eq(warnings, 0):
        mid_eng = "High warnings in the portfolio are raising risk flags. "
        mid_esp = "Altas advertencias en el portafolio están generando alertas de riesgo. "
    elif score_eq(quality, 0):
        mid_eng = "Technical quality across the portfolio is concerning. "
        mid_esp = "La calidad técnica del portafolio es preocupante. "
    else:
        mid_eng = "The owner demonstrates reasonable historical commitment. "
        mid_esp = "El owner demuestra un compromiso histórico razonable. "

    if score_lt(total, 11):
        tail_eng = "high risk for long-term partnerships."
        tail_esp = "alto riesgo para partnerships a largo plazo."
    elif score_lt(total, 15):
        tail_eng = "moderate to high risk due to stability issues."
        tail_esp = "riesgo moderado a alto por problemas de estabilidad."
    elif score_lt(total, Decimal("18.5")):
        tail_eng = "moderate risk with room for improvement."
        tail_esp = "riesgo moderado con espacio para mejorar."
    else:
        tail_eng = "strong historical credibility and low risk."
        tail_esp = "fuerte credibilidad histórica y bajo riesgo."

    business_eng = (
        "This History pillar evaluates the owner's longevity, wallet stability, "
        "and the overall quality of their agent portfolio. "
        + mid_eng
        + "Overall, the current level suggests "
        + tail_eng
    )
    business_esp = (
        "Este pilar History evalúa la longevidad del owner, la estabilidad de su "
        "wallet y la calidad general de su portafolio de agentes. "
        + mid_esp
        + "En general, el nivel actual sugiere "
        + tail_esp
    )

    strengths_eng: list[str] = []
    strengths_esp: list[str] = []
    if score_ge(antiquity, 2):
        strengths_eng.append("Proven owner longevity and ecosystem experience")
        strengths_esp.append("Antigüedad probada del owner y experiencia en el ecosistema")
    if score_eq(active_pf, 3):
        strengths_eng.append("Healthy active portfolio with strong management")
        strengths_esp.append("Portafolio activo saludable con buena gestión")
    if score_eq(adv_audits, 2):
        strengths_eng.append("Excellent external audit coverage across portfolio")
        strengths_esp.append("Excelente cobertura de auditorías externas en el portafolio")

    concerns_eng: list[str] = []
    concerns_esp: list[str] = []
    if score_eq(stability, 0):
        concerns_eng.append("Frequent ownership changes detected")
        concerns_esp.append("Múltiples cambios de propiedad detectados")
    if score_eq(warnings, 0):
        concerns_eng.append("High number of warnings in the portfolio")
        concerns_esp.append("Alto número de advertencias en el portafolio")
    if score_eq(quality, 0):
        concerns_eng.append("Low technical quality and services adoption in portfolio")
        concerns_esp.append("Baja calidad técnica y adopción de servicios en el portafolio")

    if score_eq(stability, 0):
        rec_eng = (
            "Priority action: Stabilize ownership by minimizing future owner changes "
            "and clearly communicating long-term commitment to the ecosystem."
        )
        rec_esp = (
            "Acción prioritaria: Estabilizar la propiedad minimizando cambios futuros "
            "de owner y comunicando claramente el compromiso a largo plazo."
        )
    elif score_eq(warnings, 0):
        rec_eng = (
            "Focus on cleaning the portfolio by addressing warnings, especially "
            "possible bots and spam attestations."
        )
        rec_esp = (
            "Enfocarse en limpiar el portafolio resolviendo advertencias, especialmente "
            "posibles bots y attestations spam."
        )
    elif score_eq(quality, 0):
        rec_eng = (
            "Improve technical quality across the portfolio by enhancing metadata "
            "richness and adding more services to owned agents."
        )
        rec_esp = (
            "Mejorar la calidad técnica del portafolio aumentando la riqueza de "
            "metadatos y añadiendo más servicios a los agentes del owner."
        )
    elif score_eq(adv_audits, 0):
        rec_eng = (
            "Increase external audit coverage across the portfolio to build stronger "
            "third-party credibility."
        )
        rec_esp = (
            "Aumentar la cobertura de auditorías externas en el portafolio para "
            "construir mayor credibilidad de terceros."
        )
    else:
        rec_eng = (
            "Continue maintaining strong portfolio health and consider expanding "
            "high-quality external audits and paid activity."
        )
        rec_esp = (
            "Continuar manteniendo una buena salud del portafolio y considerar "
            "expandir auditorías externas de calidad y actividad pagada."
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
    "owner_wallet_active_reason": render_owner_wallet_active_reason,
    "agent_owner_stability_reason": render_agent_owner_stability_reason,
    "owner_antiquity_reason": render_owner_antiquity_reason,
    "portafolio_agent_active_reason": render_portafolio_agent_active_reason,
    "portafolio_basic_external_audits_reason": render_portafolio_basic_external_audits_reason,
    "portafolio_warnings_reason": render_portafolio_warnings_reason,
    "portafolio_quality_agent_reason": render_portafolio_quality_agent_reason,
    "portafolio_advanced_external_audits_reason": render_portafolio_advanced_external_audits_reason,
    "portafolio_general_activity_reason": render_portafolio_general_activity_reason,
}


def render_history_reasons(ctx: dict[str, Any], scores: dict[str, Any]) -> dict[str, Any]:
    """Devuelve {reason_column: reason_dict|None, ..., 'pillar_summary': dict}."""
    out: dict[str, Any] = {}
    for item in (*HISTORY.basic, *HISTORY.intermediate, *HISTORY.advanced):
        renderer = _ITEM_RENDERERS[item.reason_column]
        out[item.reason_column] = renderer(ctx, scores.get(item.score_column))
    out["pillar_summary"] = render_history_pillar_summary(ctx, scores)
    return out


render_pillar_history = render_history_reasons


def _pillar_total(scores: dict[str, Any]) -> Decimal:
    if scores.get("pillar_score") is not None:
        return to_decimal(scores["pillar_score"])
    blocks = (
        scores.get("block_basic_score"),
        scores.get("block_intermediate_score"),
        scores.get("block_advanced_score"),
    )
    if all(b is not None for b in blocks):
        return sum((to_decimal(b) for b in blocks), Decimal(0))
    total = Decimal(0)
    for item in (*HISTORY.basic, *HISTORY.intermediate, *HISTORY.advanced):
        total += to_decimal(scores.get(item.score_column), 0)
    return total


def _verify_score(label: str, passed: Any, expected: Any) -> None:
    if passed is None:
        return
    if not score_eq(passed, expected):
        logger.debug(
            "history score mismatch item=%s passed=%s expected=%s (emitting from inputs)",
            label,
            passed,
            expected,
        )
