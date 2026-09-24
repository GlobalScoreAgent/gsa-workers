"""Render de reasons + pillar_summary del pilar Usage (paridad SQL).

Fuente canónica: index_humi.agent_pillar_usage_calculate
(scripts/agent_pillar_usage_calculate.sql / migration multichain usage).

No recalcula scores para decidir el texto cuando SQL hace CASE WHEN score = X:
usa el score persistido en `scores`. El control flow de IFs sobre inputs replica SQL.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from pillar_spec import USAGE

from .util import (
    as_mapping,
    as_of_date,
    build_reason,
    coalesce,
    days_since,
    jsonb_agg,
    score_eq,
    score_ge,
    score_lt,
    sql_round,
    to_decimal,
    whole_number,
)

logger = logging.getLogger("humi_reason_publisher.render.usage")


def _summary(ctx: dict[str, Any], key: str) -> dict[str, Any]:
    return as_mapping(ctx.get(key)) or {}


def _mapping_get_optional(mapping: dict[str, Any], key: str) -> Decimal | None:
    """Replica (jsonb->>'key')::numeric: NULL si falta o es null."""
    if key not in mapping or mapping[key] is None:
        return None
    return to_decimal(mapping[key])


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "t", "1", "yes")
    return bool(value)


def _agent_created_at(ctx: dict[str, Any]) -> Any:
    return coalesce(ctx.get("agent_created_at"), ctx.get("on_chain_created_at"))


def _agent_age_days(ctx: dict[str, Any]) -> int:
    created = _agent_created_at(ctx)
    if created is None:
        return 0
    return days_since(created, as_of_date(ctx))


def _onchain_type_counts(ctx: dict[str, Any]) -> tuple[int, int, int, int]:
    att = _summary(ctx, "attestations_summary")
    exe = _summary(ctx, "on_chain_executions_summary")
    fb = _summary(ctx, "on_chain_feedbacks_summary")
    prot = _summary(ctx, "protocol_activity_summary")
    return (
        int(to_decimal(att.get("valid_count"), 0)),
        int(to_decimal(exe.get("valid_count"), 0)),
        int(to_decimal(fb.get("valid_count"), 0)),
        int(to_decimal(prot.get("valid_count"), 0)),
    )


def _onchain_avg_and_count(ctx: dict[str, Any]) -> tuple[int, Decimal | None]:
    att_valid, exec_valid, fb_valid, prot_valid = _onchain_type_counts(ctx)
    onchain_count = (
        (1 if att_valid > 0 else 0)
        + (1 if exec_valid > 0 else 0)
        + (1 if fb_valid > 0 else 0)
        + (1 if prot_valid > 0 else 0)
    )

    att = _summary(ctx, "attestations_summary")
    fb = _summary(ctx, "on_chain_feedbacks_summary")
    prot = _summary(ctx, "protocol_activity_summary")
    att_avg = _mapping_get_optional(att, "avg_score")
    fb_avg = _mapping_get_optional(fb, "avg_score")
    prot_avg = _mapping_get_optional(prot, "avg_score")

    denom = (
        (1 if att_avg is not None else 0)
        + (1 if fb_avg is not None else 0)
        + (1 if prot_avg is not None else 0)
    )
    if denom == 0:
        return onchain_count, None

    total = (
        to_decimal(coalesce(att_avg, 0))
        + to_decimal(coalesce(fb_avg, 0))
        + to_decimal(coalesce(prot_avg, 0))
    )
    return onchain_count, total / denom


# ---------------------------------------------------------------------------
# Basic
# ---------------------------------------------------------------------------


def render_basic_activity_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    wallet = _summary(ctx, "wallet_tx_summary")
    nonce_current = to_decimal(wallet.get("nonce_current"), 0)
    nonce_delta_30 = to_decimal(wallet.get("nonce_delta_30_days"), 0)
    span_days = int(to_decimal(wallet.get("nonce_history_span_days"), 0))
    age_days = _agent_age_days(ctx)
    att_valid, exec_valid, fb_valid, prot_valid = _onchain_type_counts(ctx)
    onchain_types = (
        (1 if att_valid > 0 else 0)
        + (1 if exec_valid > 0 else 0)
        + (1 if fb_valid > 0 else 0)
        + (1 if prot_valid > 0 else 0)
    )

    use_stock = age_days < 30 or span_days < 30

    if use_stock:
        if nonce_current > 0:
            _verify_score("basic_activity", score, 10)
            grace_type = (
                "short_observation_grace"
                if span_days < 30 and age_days >= 30
                else "new_agent"
            )
            return build_reason(
                "On-chain nonce stock present; scoring with current nonce because "
                "agent is new or observation history is shorter than 30 days.",
                "Hay stock de nonce on-chain; se puntua con nonce actual porque el "
                "agente es nuevo o el historial de observacion es menor a 30 dias.",
                type=grace_type,
                age_days=age_days,
                nonce_history_span_days=span_days,
                nonce_current=whole_number(nonce_current),
            )
        _verify_score("basic_activity", score, 0)
        return build_reason(
            "No current wallet nonce; low initial engagement or inactivity.",
            "Sin nonce actual en wallet; bajo engagement inicial o inactividad.",
            nonce_history_span_days=span_days,
        )

    if nonce_delta_30 > 0 or onchain_types >= 2:
        _verify_score("basic_activity", score, 10)
        return build_reason(
            "Established agent demonstrating consistent recent activity across "
            "multiple on-chain channels, reflecting healthy operational maturity.",
            "Agente establecido demostrando actividad reciente consistente en "
            "multiples canales on-chain, reflejando madurez operativa saludable.",
            type="established_agent",
            age_days=age_days,
            nonce_history_span_days=span_days,
            nonce_delta_30d=whole_number(nonce_delta_30),
            onchain_types=onchain_types,
        )

    _verify_score("basic_activity", score, 0)
    return build_reason(
        "No meaningful activity detected in the last 30 days, indicating potential "
        "inactivity or low engagement risk.",
        "No se detecta actividad significativa en los ultimos 30 dias, indicando "
        "posible inactividad o bajo riesgo de engagement.",
        nonce_history_span_days=span_days,
    )


# ---------------------------------------------------------------------------
# Intermediate
# ---------------------------------------------------------------------------


def render_wallet_intermediate_activity_reason(
    ctx: dict[str, Any], score: Any
) -> dict[str, Any]:
    wallet = _summary(ctx, "wallet_tx_summary")
    nonce_current = to_decimal(wallet.get("nonce_current"), 0)
    nonce_delta_15 = to_decimal(wallet.get("nonce_delta_15_days"), 0)
    span_days = int(to_decimal(wallet.get("nonce_history_span_days"), 0))
    age_days = _agent_age_days(ctx)

    use_stock = age_days < 15 or span_days < 15
    score_value = nonce_current if use_stock else nonce_delta_15
    nonce_used = "nonce_current" if use_stock else "nonce_delta_15_days"

    if score_value >= 301:
        expected: Decimal | int | float = Decimal("2.5")
    elif score_value >= 101:
        expected = Decimal("1.5")
    elif score_value >= 1:
        expected = Decimal("0.75")
    else:
        expected = 0
    _verify_score("wallet_intermediate_activity", score, expected)

    if score_eq(score, Decimal("2.5")):
        eng = "Strong wallet activity demonstrating healthy transactional volume."
        esp = "Fuerte actividad en wallet demostrando volumen transaccional saludable."
    elif score_eq(score, Decimal("1.5")):
        eng = "Moderate wallet activity showing acceptable engagement levels."
        esp = "Actividad moderada en wallet mostrando niveles aceptables de engagement."
    elif score_eq(score, Decimal("0.75")):
        eng = "Minimal wallet activity, indicating early-stage or low-intensity usage."
        esp = (
            "Actividad minima en wallet, indicando uso en etapa inicial o de "
            "baja intensidad."
        )
    else:
        eng = (
            "No meaningful wallet activity detected, raising concerns about "
            "operational engagement."
        )
        esp = (
            "No se detecta actividad significativa en wallet, generando preocupacion "
            "sobre el engagement operativo."
        )

    return build_reason(
        eng,
        esp,
        age_days=age_days,
        nonce_history_span_days=span_days,
        short_observation_grace=use_stock and age_days >= 15,
        nonce_used=nonce_used,
        value=whole_number(score_value),
    )


def render_on_chain_intermediate_activity_reason(
    ctx: dict[str, Any], score: Any
) -> dict[str, Any]:
    onchain_count, onchain_avg = _onchain_avg_and_count(ctx)
    avg_metric = sql_round(coalesce(onchain_avg, 0), 2)

    if onchain_count >= 2:
        avg = to_decimal(coalesce(onchain_avg, 0))
        if avg >= 90:
            expected: Decimal | int | float = 4
        elif avg >= 70:
            expected = Decimal("3.2")
        elif avg >= 50:
            expected = Decimal("2.4")
        elif avg >= 30:
            expected = 2
        elif avg >= 10:
            expected = Decimal("1.2")
        else:
            expected = 0
    else:
        expected = 0
    _verify_score("on_chain_intermediate_activity", score, expected)

    if score_eq(score, 4):
        eng = (
            "Outstanding on-chain activity across multiple channels with very high "
            "quality scores."
        )
        esp = (
            "Actividad on-chain sobresaliente en múltiples canales con puntuaciones "
            "de calidad muy altas."
        )
    elif score_ge(score, Decimal("3.2")):
        eng = "Strong on-chain activity with good quality and diversity."
        esp = "Fuerte actividad on-chain con buena calidad y diversidad."
    elif score_ge(score, Decimal("2.4")):
        eng = "Moderate on-chain activity with acceptable quality."
        esp = "Actividad on-chain moderada con calidad aceptable."
    else:
        eng = (
            "Limited or low-quality on-chain activity, indicating room for "
            "operational improvement."
        )
        esp = (
            "Actividad on-chain limitada o de baja calidad, indicando espacio para "
            "mejora operativa."
        )

    return build_reason(
        eng,
        esp,
        active_types=onchain_count,
        avg_score=avg_metric,
    )


def render_comment_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    comments = _summary(ctx, "comments_summary")
    comments_valid = int(to_decimal(comments.get("valid_count"), 0))
    comments_revoke = int(to_decimal(comments.get("revoke_count"), 0))

    if comments_valid >= 1 and comments_revoke == 0:
        _verify_score("comment", score, 1)
        return build_reason(
            "Positive recent feedback received with no revokes, strengthening "
            "community trust and validation of the agent.",
            "Feedback positivo reciente recibido sin revocaciones, fortaleciendo la "
            "confianza comunitaria y la validación del agente.",
            comments_30d=comments_valid,
        )

    _verify_score("comment", score, 0)
    return build_reason(
        "No recent positive comments or presence of revokes, reducing social proof "
        "and perceived reliability.",
        "Sin comentarios positivos recientes o presencia de revocaciones, reduciendo "
        "la prueba social y la confiabilidad percibida.",
        comments_30d=comments_valid,
        revoke=comments_revoke,
    )


def render_multichain_valuable_usage_reason(
    ctx: dict[str, Any], score: Any
) -> dict[str, Any]:
    multi = _summary(ctx, "multichain_data")
    valuable_chains = int(to_decimal(multi.get("valuable_chains_count"), 0))
    shallow = _as_bool(multi.get("shallow_multichain_spread"), False)

    if valuable_chains >= 2:
        expected: Decimal | int | float = Decimal("2.5")
    elif valuable_chains == 1:
        expected = Decimal("1.5")
    elif shallow:
        expected = Decimal("-1.0")
    else:
        expected = 0
    _verify_score("multichain_valuable_usage", score, expected)

    if score_eq(score, Decimal("2.5")):
        eng = "Excellent valuable multichain usage across 2+ high-quality chains."
        esp = "Excelente uso valioso multichain en 2+ chains de alta calidad."
    elif score_eq(score, Decimal("1.5")):
        eng = "Good valuable usage concentrated in one high-quality chain."
        esp = "Buen uso valioso concentrado en una chain de alta calidad."
    elif score_eq(score, Decimal("-1.0")):
        eng = "Superficial activity spread across too many low-value chains."
        esp = "Actividad superficial dispersa en demasiadas chains de bajo valor."
    else:
        eng = "No significant valuable multichain usage signal detected."
        esp = "No se detecta señal significativa de uso valioso multichain."

    return build_reason(
        eng,
        esp,
        valuable_chains_count=valuable_chains,
        shallow_multichain_spread=shallow,
    )


# ---------------------------------------------------------------------------
# Advanced
# ---------------------------------------------------------------------------


def render_wallet_advanced_activity_reason(
    ctx: dict[str, Any], score: Any
) -> dict[str, Any]:
    wallet = _summary(ctx, "wallet_tx_summary")
    nonce_current = to_decimal(wallet.get("nonce_current"), 0)
    nonce_delta_7 = to_decimal(wallet.get("nonce_delta_7_days"), 0)
    span_days = int(to_decimal(wallet.get("nonce_history_span_days"), 0))
    age_days = _agent_age_days(ctx)

    use_stock = age_days == 0 or span_days < 7
    score_value = nonce_current if use_stock else nonce_delta_7
    nonce_used = "nonce_current" if use_stock else "nonce_delta_7_days"
    short_grace = use_stock and age_days > 0

    if score_value >= 500:
        _verify_score("wallet_advanced_activity", score, 1)
        return build_reason(
            "Very high wallet activity qualifying as advanced.",
            "Actividad en wallet muy alta que califica como avanzada.",
            nonce_history_span_days=span_days,
            short_observation_grace=short_grace,
            nonce_used=nonce_used,
            value=whole_number(score_value),
        )

    _verify_score("wallet_advanced_activity", score, 0)
    return build_reason(
        "Insufficient wallet activity to qualify as advanced.",
        "Actividad en wallet insuficiente para calificar como avanzada.",
        nonce_history_span_days=span_days,
        short_observation_grace=short_grace,
        nonce_used=nonce_used,
        value=whole_number(score_value),
    )


def render_on_chain_advanced_activity_reason(
    ctx: dict[str, Any], score: Any
) -> dict[str, Any]:
    onchain_count, onchain_avg = _onchain_avg_and_count(ctx)
    avg_metric = sql_round(coalesce(onchain_avg, 0), 2)

    if onchain_count >= 3:
        avg = to_decimal(coalesce(onchain_avg, 0))
        if avg >= 80:
            expected: Decimal | int | float = Decimal("2.5")
        elif avg >= 70:
            expected = Decimal("1.5")
        elif avg >= 60:
            expected = Decimal("0.75")
        else:
            expected = 0
    else:
        expected = 0
    _verify_score("on_chain_advanced_activity", score, expected)

    if score_eq(score, Decimal("2.5")):
        eng = (
            "Outstanding advanced on-chain activity across 3+ channels with very "
            "high quality scores."
        )
        esp = (
            "Actividad on-chain avanzada sobresaliente en 3+ canales con "
            "puntuaciones de calidad muy altas."
        )
    elif score_eq(score, Decimal("1.5")):
        eng = "Strong advanced on-chain activity with good quality and diversity."
        esp = "Fuerte actividad on-chain avanzada con buena calidad y diversidad."
    elif score_eq(score, Decimal("0.75")):
        eng = "Moderate advanced on-chain activity with acceptable quality."
        esp = "Actividad on-chain avanzada moderada con calidad aceptable."
    else:
        eng = (
            "Limited advanced on-chain activity, indicating room for operational "
            "growth."
        )
        esp = (
            "Actividad on-chain avanzada limitada, indicando espacio para "
            "crecimiento operativo."
        )

    return build_reason(
        eng,
        esp,
        active_types=onchain_count,
        avg_score=avg_metric,
    )


def render_on_chain_activity_paid_reason(
    ctx: dict[str, Any], score: Any
) -> dict[str, Any]:
    prot = _summary(ctx, "protocol_activity_summary")
    prot_valid = int(to_decimal(prot.get("valid_count"), 0))
    prot_payments = int(to_decimal(prot.get("valid_payment_count"), 0))

    if prot_valid >= 2 and prot_payments >= 1:
        _verify_score("on_chain_activity_paid", score, Decimal("1.5"))
        return build_reason(
            "Strong paid protocol activity detected, demonstrating real economic "
            "usage and business value generation.",
            "Fuerte actividad pagada en protocolos detectada, demostrando uso "
            "económico real y generación de valor de negocio.",
            protocol_30d=prot_valid,
            with_payments=prot_payments,
        )

    _verify_score("on_chain_activity_paid", score, 0)
    return build_reason(
        "Insufficient paid protocol activity, limiting demonstrated economic "
        "engagement.",
        "Actividad pagada en protocolos insuficiente, limitando el engagement "
        "económico demostrado.",
        protocol_30d=prot_valid,
        with_payments=prot_payments,
    )


def render_multichain_high_value_consistency_reason(
    ctx: dict[str, Any], score: Any
) -> dict[str, Any]:
    multi = _summary(ctx, "multichain_data")
    high_value = _as_bool(multi.get("has_high_value_chain_presence"), False)
    valuable_chains = int(to_decimal(multi.get("valuable_chains_count"), 0))
    concentration = to_decimal(multi.get("usage_concentration_ratio"), 0)

    if high_value and valuable_chains >= 2:
        expected: Decimal | int | float = Decimal("1.5")
    elif high_value:
        expected = 1
    elif concentration > to_decimal("0.90"):
        expected = Decimal("-0.5")
    else:
        expected = 0
    _verify_score("multichain_high_value_consistency", score, expected)

    if score_eq(score, Decimal("1.5")):
        eng = (
            "Strong multichain consistency with activity in multiple high-value "
            "chains."
        )
        esp = (
            "Fuerte consistencia multichain con actividad en múltiples chains de "
            "alto valor."
        )
    elif score_eq(score, 1):
        eng = "Good presence and consistency in at least one high-value chain."
        esp = "Buena presencia y consistencia en al menos una chain de alto valor."
    elif score_eq(score, Decimal("-0.5")):
        eng = "Extremely concentrated usage in a single chain."
        esp = "Uso extremadamente concentrado en una sola chain."
    else:
        eng = "No significant multichain high-value consistency signal detected."
        esp = "No se detecta señal significativa de consistencia multichain de alto valor."

    return build_reason(
        eng,
        esp,
        has_high_value_chain_presence=high_value,
        valuable_chains_count=valuable_chains,
        usage_concentration_ratio=sql_round(concentration, 4),
    )


# ---------------------------------------------------------------------------
# Penalty
# ---------------------------------------------------------------------------


def render_penalty_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    comments = _summary(ctx, "comments_summary")
    exe = _summary(ctx, "on_chain_executions_summary")
    fb = _summary(ctx, "on_chain_feedbacks_summary")
    prot = _summary(ctx, "protocol_activity_summary")
    att = _summary(ctx, "attestations_summary")

    has_revoke = (
        int(to_decimal(comments.get("revoke_count"), 0)) > 0
        or int(to_decimal(exe.get("revoke_count"), 0)) > 0
        or int(to_decimal(fb.get("revoke_count"), 0)) > 0
        or int(to_decimal(prot.get("revoke_count"), 0)) > 0
        or int(to_decimal(att.get("revoke_count"), 0)) > 0
    )

    if has_revoke:
        _verify_score("penalty", score, Decimal("-1.5"))
        return build_reason(
            "Recent revokes or warnings detected, negatively impacting perceived "
            "reliability and trust.",
            "Revocaciones o advertencias recientes detectadas, impactando "
            "negativamente la confiabilidad y confianza percibida.",
        )

    _verify_score("penalty", score, 0)
    return build_reason(
        "No recent revokes or warnings, supporting clean and trustworthy activity "
        "profile.",
        "Sin revocaciones ni advertencias recientes, apoyando un perfil de "
        "actividad limpio y confiable.",
    )


# ---------------------------------------------------------------------------
# Pillar summary
# ---------------------------------------------------------------------------


def _score_lt_known(score: Any, target: Any) -> bool:
    """Como score_lt, pero None no cuenta como menor (scores opcionales)."""
    if score is None:
        return False
    return score_lt(score, target)


def render_usage_pillar_summary(ctx: dict[str, Any], scores: dict[str, Any]) -> dict[str, Any]:
    """Replica v_pillar_summary; omite last_calculated a propósito (Stage 2)."""
    _ = ctx
    total = _pillar_total(scores)
    penalty = scores.get("penalty_score")
    basic = scores.get("basic_activity_score")
    multichain_valuable = scores.get("multichain_valuable_usage_score")
    multichain_consistency = scores.get("multichain_high_value_consistency_score")
    paid = scores.get("on_chain_activity_paid_score")
    on_chain_inter = scores.get("on_chain_intermediate_activity_score")
    on_chain_adv = scores.get("on_chain_advanced_activity_score")

    if score_ge(total, 22):
        overall_eng = (
            "The agent demonstrates outstanding usage maturity and strong "
            "operational engagement."
        )
        overall_esp = (
            "El agente demuestra una madurez de uso sobresaliente y fuerte "
            "engagement operativo."
        )
    elif score_ge(total, Decimal("18.5")):
        overall_eng = (
            "The agent shows solid and consistent usage patterns with good "
            "operational health."
        )
        overall_esp = (
            "El agente muestra patrones de uso sólidos y consistentes con buena "
            "salud operativa."
        )
    elif score_ge(total, 15):
        overall_eng = (
            "The agent has acceptable usage levels but presents opportunities for "
            "stronger engagement."
        )
        overall_esp = (
            "El agente tiene niveles de uso aceptables pero presenta oportunidades "
            "para mayor engagement."
        )
    elif score_ge(total, 11):
        overall_eng = (
            "The agent has weak to moderate usage. Activity appears limited or "
            "inconsistent."
        )
        overall_esp = (
            "El agente tiene uso débil a moderado. La actividad parece limitada o "
            "inconsistente."
        )
    else:
        overall_eng = (
            "The agent exhibits very low usage. Significant concerns regarding "
            "operational activity and engagement."
        )
        overall_esp = (
            "El agente presenta un uso muy bajo. Existen preocupaciones importantes "
            "sobre su actividad operativa y engagement."
        )

    if _score_lt_known(penalty, 0):
        mid_eng = "Recent revokes are damaging trust and perceived reliability. "
        mid_esp = (
            "Las revocaciones recientes están dañando la confianza y la "
            "confiabilidad percibida. "
        )
    elif score_eq(basic, 0):
        mid_eng = "The agent shows concerningly low recent activity. "
        mid_esp = "El agente muestra una actividad reciente preocupantemente baja. "
    elif score_ge(multichain_valuable, 2):
        mid_eng = " Strong valuable multichain usage improves operational robustness. "
        mid_esp = " Un fuerte uso valioso multichain mejora la robustez operativa. "
    elif score_ge(multichain_consistency, Decimal("1.5")):
        mid_eng = (
            " Excellent consistency across high-value chains adds strategic value. "
        )
        mid_esp = (
            " Una excelente consistencia en chains de alto valor aporta valor "
            "estratégico. "
        )
    elif _score_lt_known(multichain_valuable, 0):
        mid_eng = " Weak multichain quality reduces demonstrated operational maturity. "
        mid_esp = " La calidad multichain débil reduce la madurez operativa demostrada. "
    elif score_eq(paid, 0):
        mid_eng = (
            "Lack of paid protocol activity limits demonstrated economic value. "
        )
        mid_esp = (
            "La falta de actividad pagada en protocolos limita el valor económico "
            "demostrado. "
        )
    elif _score_lt_known(on_chain_inter, Decimal("2.4")):
        mid_eng = (
            "Limited diversity in on-chain actions reduces operational robustness. "
        )
        mid_esp = (
            "Diversidad limitada en acciones on-chain reduce la robustez operativa. "
        )
    else:
        mid_eng = "The agent maintains a healthy operational rhythm. "
        mid_esp = "El agente mantiene un ritmo operativo saludable. "

    if score_lt(total, 11):
        tail_eng = "high risk of inactivity or low adoption."
        tail_esp = "alto riesgo de inactividad o baja adopción."
    elif score_lt(total, 15):
        tail_eng = "moderate risk with limited momentum."
        tail_esp = "riesgo moderado con momentum limitado."
    elif score_lt(total, Decimal("18.5")):
        tail_eng = "acceptable but not standout engagement."
        tail_esp = "engagement aceptable pero no destacado."
    else:
        tail_eng = "strong operational presence and healthy usage."
        tail_esp = "fuerte presencia operativa y uso saludable."

    business_eng = (
        "This Usage pillar measures the agent's real operational activity, "
        "consistency, and value generation through on-chain interactions. "
        + mid_eng
        + "Overall, the current level suggests "
        + tail_eng
    )
    business_esp = (
        "Este pilar Usage mide la actividad operativa real del agente, su "
        "consistencia y generación de valor mediante interacciones on-chain. "
        + mid_esp
        + "En general, el nivel actual sugiere "
        + tail_esp
    )

    strengths_eng: list[str] = []
    strengths_esp: list[str] = []
    if score_eq(paid, Decimal("1.5")):
        strengths_eng.append(
            "Demonstrated paid protocol usage generating real economic value"
        )
        strengths_esp.append("Uso pagado en protocolos que genera valor económico real")
    if score_ge(on_chain_adv, Decimal("1.5")):
        strengths_eng.append(
            "Diverse and high-quality on-chain activity across multiple types"
        )
        strengths_esp.append(
            "Actividad on-chain diversa y de alta calidad en múltiples tipos"
        )
    if score_eq(basic, 10):
        strengths_eng.append("Consistent and healthy operational rhythm")
        strengths_esp.append("Ritmo operativo consistente y saludable")
    if score_ge(multichain_valuable, 2):
        strengths_eng.append(
            "Strong valuable multichain usage across high-quality chains"
        )
        strengths_esp.append("Fuerte uso valioso multichain en chains de alta calidad")
    if score_ge(multichain_consistency, Decimal("1.5")):
        strengths_eng.append("Excellent multichain consistency in high-value chains")
        strengths_esp.append(
            "Excelente consistencia multichain en chains de alto valor"
        )

    concerns_eng: list[str] = []
    concerns_esp: list[str] = []
    if _score_lt_known(penalty, 0):
        concerns_eng.append("Recent revokes detected - negatively impacting trust")
        concerns_esp.append(
            "Revocaciones recientes detectadas - impactando negativamente la confianza"
        )
    if score_eq(basic, 0):
        concerns_eng.append("Very low or no recent activity detected")
        concerns_esp.append("Actividad reciente muy baja o nula")
    if score_eq(paid, 0):
        concerns_eng.append("No paid protocol activity detected")
        concerns_esp.append("No se detecta actividad pagada en protocolos")
    if _score_lt_known(on_chain_inter, 2):
        concerns_eng.append("Limited on-chain diversity and engagement")
        concerns_esp.append("Diversidad y engagement on-chain limitados")
    if _score_lt_known(multichain_valuable, 0):
        concerns_eng.append("Superficial or low-value multichain activity detected")
        concerns_esp.append(
            "Actividad multichain superficial o de bajo valor detectada"
        )
    if _score_lt_known(multichain_consistency, 0):
        concerns_eng.append("Extremely concentrated usage in a single chain")
        concerns_esp.append("Uso extremadamente concentrado en una sola chain")

    if _score_lt_known(penalty, 0):
        rec_eng = (
            "Priority: Eliminate all recent revokes and warnings to restore trust "
            "and operational credibility."
        )
        rec_esp = (
            "Prioridad: Eliminar todas las revocaciones y advertencias recientes "
            "para restaurar la confianza y credibilidad operativa."
        )
    elif score_eq(basic, 0):
        rec_eng = (
            "Priority: Generate consistent on-chain activity immediately to "
            "demonstrate real operational engagement."
        )
        rec_esp = (
            "Prioridad: Generar actividad on-chain consistente de inmediato para "
            "demostrar engagement operativo real."
        )
    elif score_eq(paid, 0):
        rec_eng = (
            "Focus on implementing paid protocol interactions to prove real "
            "economic usage and business value."
        )
        rec_esp = (
            "Enfocarse en implementar interacciones pagadas en protocolos para "
            "demostrar uso económico real y valor de negocio."
        )
    elif _score_lt_known(on_chain_inter, 2):
        rec_eng = (
            "Increase diversity of on-chain actions (attestations, executions, "
            "feedbacks, protocols) to strengthen operational profile."
        )
        rec_esp = (
            "Aumentar la diversidad de acciones on-chain (attestations, ejecuciones, "
            "feedbacks, protocolos) para fortalecer el perfil operativo."
        )
    elif _score_lt_known(multichain_valuable, 0):
        rec_eng = (
            "Focus on generating meaningful activity in at least 2 high-value chains."
        )
        rec_esp = (
            "Enfocarse en generar actividad significativa en al menos 2 chains de "
            "alto valor."
        )
    elif _score_lt_known(multichain_consistency, 0):
        rec_eng = (
            "Reduce extreme concentration in a single chain and expand to other "
            "valuable chains."
        )
        rec_esp = (
            "Reducir la concentración extrema en una sola chain y expandir hacia "
            "otras chains valiosas."
        )
    else:
        rec_eng = (
            "Continue building momentum by maintaining consistent activity and "
            "expanding paid protocol usage."
        )
        rec_esp = (
            "Continuar construyendo momentum manteniendo actividad consistente y "
            "expandiendo el uso de protocolos pagados."
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
    "basic_activity_reason": render_basic_activity_reason,
    "wallet_intermediate_activity_reason": render_wallet_intermediate_activity_reason,
    "on_chain_intermediate_activity_reason": render_on_chain_intermediate_activity_reason,
    "comment_reason": render_comment_reason,
    "multichain_valuable_usage_reason": render_multichain_valuable_usage_reason,
    "wallet_advanced_activity_reason": render_wallet_advanced_activity_reason,
    "on_chain_advanced_activity_reason": render_on_chain_advanced_activity_reason,
    "on_chain_activity_paid_reason": render_on_chain_activity_paid_reason,
    "multichain_high_value_consistency_reason": (
        render_multichain_high_value_consistency_reason
    ),
}


def render_usage_reasons(ctx: dict[str, Any], scores: dict[str, Any]) -> dict[str, Any]:
    """Devuelve {reason_column: reason_dict, ..., penalty_reason, pillar_summary}."""
    out: dict[str, Any] = {}
    for item in (*USAGE.basic, *USAGE.intermediate, *USAGE.advanced):
        renderer = _ITEM_RENDERERS[item.reason_column]
        out[item.reason_column] = renderer(ctx, scores.get(item.score_column))
    out["penalty_reason"] = render_penalty_reason(ctx, scores.get("penalty_score"))
    out["pillar_summary"] = render_usage_pillar_summary(ctx, scores)
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
        return total + to_decimal(scores.get("penalty_score"), 0)
    total = Decimal(0)
    for item in (*USAGE.basic, *USAGE.intermediate, *USAGE.advanced):
        total += to_decimal(scores.get(item.score_column), 0)
    total += to_decimal(scores.get("penalty_score"), 0)
    return total


def _verify_score(label: str, passed: Any, expected: Any) -> None:
    if passed is None:
        return
    if not score_eq(passed, expected):
        logger.debug(
            "usage score mismatch item=%s passed=%s expected=%s (emitting from inputs)",
            label,
            passed,
            expected,
        )
