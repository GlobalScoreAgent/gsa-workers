"""Render de reasons + pillar_summary del pilar Information (paridad SQL).

Fuente canónica:
- migrations/20260804020000_pillar_information_description_quality.sql (FOR loop)
- migrations/20260804010000_description_is_low_quality.sql (description_quality_reason)

No recalcula scores para decidir el texto cuando SQL hace CASE WHEN score:
usa el score persistido en `scores`. Los IFs sobre inputs (name/desc/image/profiles)
replican el control flow SQL.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from typing import Any

from pillar_spec import INFORMATION

from .util import (
    as_mapping,
    build_reason,
    coalesce,
    jsonb_agg,
    mapping_get_num,
    score_eq,
    score_ge,
    score_gt,
    score_lt,
    to_decimal,
)

logger = logging.getLogger("humi_reason_publisher.render.information")

_IMAGE_EXT_RE = re.compile(r"\.(png|jpg|jpeg|webp|svg|gif|bmp)$", re.IGNORECASE)
_NAME_DUMMY_WORD_RE = re.compile(r"\b(dummy|fake|spam|placeholder)\b")
_NAME_PREFIX_RES = (
    re.compile(r"^test[-_ ]"),
    re.compile(r"^demo[-_ ]"),
    re.compile(r"^example[-_ ]"),
    re.compile(r"^temp[-_ ]"),
)
_SUSPICIOUS_NAMES = frozenset(
    {"test-agent", "test_agent", "testagent", "demo-agent", "placeholder"}
)
_CHAR_RUN_RE = re.compile(r"(.)\1{4,}")
_NGRAM_LOOP_RE = re.compile(r"(.{2,4})\1{3,}")
_ALNUM_RE = re.compile(r"[a-z0-9]")


def description_quality_reason(name: Any, description: Any) -> str | None:
    """Port de erc_8004.description_quality_reason."""
    desc = (coalesce(description, "") or "").strip()
    desc_l = desc.lower()
    name_clean = re.sub(
        r"[^a-z0-9\s]",
        "",
        (coalesce(name, "") or "").strip().lower(),
    )
    desc_len = len(desc)

    if desc_len < 10:
        return None

    name_len = len(name_clean)
    if (
        desc_len > 50
        and name_len > 0
        and (desc_len - len(desc_l.replace(name_clean, ""))) // name_len > 3
    ):
        return "name_repeat"

    if _CHAR_RUN_RE.search(desc_l):
        return "char_run"

    if desc_len >= 20 and _NGRAM_LOOP_RE.search(desc_l):
        return "ngram_loop"

    if desc_len >= 40:
        charset = len({ch for ch in desc_l if _ALNUM_RE.match(ch)})
        if charset <= 8:
            return "low_charset"

    return None


def _clean_name(name: Any) -> str:
    raw = coalesce(name, "") or ""
    return re.sub(r"[^a-zA-Z0-9\s]", "", raw).strip().lower()


def _is_nonempty_json_array(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, list):
        return len(value) > 0
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return False
        return isinstance(parsed, list) and len(parsed) > 0
    return False


def _profiles_map(ctx: dict[str, Any]) -> dict[str, Any]:
    return as_mapping(ctx.get("profiles")) or {}


def _services_breakdown(ctx: dict[str, Any]) -> dict[str, Any]:
    return as_mapping(ctx.get("services_breakdown")) or {}


def _ext_sources_count(ctx: dict[str, Any]) -> int:
    p = _profiles_map(ctx)
    return int(
        mapping_get_num(p, "agent_uri_did", 0)
        + mapping_get_num(p, "feedback_did", 0)
        + mapping_get_num(p, "feedback", 0)
        + mapping_get_num(p, "feedback_external_source", 0)
    )


def _tech_fields_count(ctx: dict[str, Any]) -> int:
    sb = _services_breakdown(ctx)
    return (
        (1 if mapping_get_num(sb, "skills", 0) > 0 else 0)
        + (1 if mapping_get_num(sb, "capabilities", 0) > 0 else 0)
        + (1 if mapping_get_num(sb, "oasf_skills", 0) > 0 else 0)
        + (1 if mapping_get_num(sb, "oasf_domains", 0) > 0 else 0)
    )


def _advanced_tech_count(ctx: dict[str, Any]) -> int:
    return (
        (1 if _is_nonempty_json_array(ctx.get("technology_stacks")) else 0)
        + (1 if _is_nonempty_json_array(ctx.get("x402s")) else 0)
        + (1 if _is_nonempty_json_array(ctx.get("technical_tools")) else 0)
        + (1 if _is_nonempty_json_array(ctx.get("technical_capabilities")) else 0)
    )


def _verify_score(label: str, passed: Any, expected: Any) -> None:
    if passed is None:
        return
    if not score_eq(passed, expected):
        logger.debug(
            "information score mismatch item=%s passed=%s expected=%s",
            label,
            passed,
            expected,
        )


def render_name_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    name_clean = _clean_name(ctx.get("name"))

    if name_clean == "" or name_clean == "unnamed agent":
        _verify_score("name", score, 0)
        return build_reason(
            "Empty or placeholder name detected, severely limiting the agent’s "
            "professional identity and trustworthiness.",
            "Nombre vacío o placeholder detectado, limitando severamente la "
            "identidad profesional y confiabilidad del agente.",
        )

    if (
        name_clean in _SUSPICIOUS_NAMES
        or any(rx.search(name_clean) for rx in _NAME_PREFIX_RES)
        or _NAME_DUMMY_WORD_RE.search(name_clean)
    ):
        _verify_score("name", score, 0)
        return build_reason(
            "Suspicious or dummy name detected, indicating low credibility and "
            "potential test/spam behavior.",
            "Nombre sospechoso o dummy detectado, indicando baja credibilidad y "
            "posible comportamiento de prueba/spam.",
        )

    expected = 3.0 if len(name_clean) >= 3 else 1.0
    _verify_score("name", score, expected)
    return build_reason(
        "Clean and professional name with sufficient length, contributing "
        "positively to the agent’s brand identity.",
        "Nombre limpio y profesional con longitud suficiente, contribuyendo "
        "positivamente a la identidad de marca del agente.",
    )


def render_description_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    desc_clean = (coalesce(ctx.get("description"), "") or "").strip()
    quality_flag = description_quality_reason(ctx.get("name"), desc_clean)

    if desc_clean == "" or len(desc_clean) < 10:
        _verify_score("description", score, 0)
        return build_reason(
            "Description is too short or empty, limiting the agent’s ability to "
            "communicate its purpose and value.",
            "La descripción es demasiado corta o está vacía, limitando la "
            "capacidad del agente para comunicar su propósito y valor.",
        )

    if quality_flag is not None:
        _verify_score("description", score, 0.9)
        return build_reason(
            "Low-quality or spammy description pattern detected, reducing "
            "perceived professionalism.",
            "Patrón de descripción de baja calidad o spam detectado, reduciendo "
            "la profesionalidad percibida.",
            quality_flag=quality_flag,
        )

    desc_len = len(desc_clean)
    if desc_len >= 80:
        expected = 3.0
    elif desc_len >= 40:
        expected = 1.8
    else:
        expected = 0.9
    _verify_score("description", score, expected)
    return build_reason(
        "Description provides meaningful context about the agent’s capabilities "
        "and purpose.",
        "La descripción proporciona contexto significativo sobre las capacidades "
        "y propósito del agente.",
        length=desc_len,
    )


def render_image_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    image_url = ctx.get("image_url")
    if image_url is not None and _IMAGE_EXT_RE.search(str(image_url)):
        _verify_score("image", score, 1.5)
        return build_reason(
            "Valid professional image URL present, significantly enhancing the "
            "agent’s visual identity and perceived legitimacy.",
            "URL de imagen profesional válida presente, mejorando significativamente "
            "la identidad visual y legitimidad percibida del agente.",
        )

    _verify_score("image", score, 0)
    return build_reason(
        "No valid image or invalid URL, reducing the agent’s visual appeal and "
        "professional presentation.",
        "Sin imagen válida o URL inválida, reduciendo el atractivo visual y la "
        "presentación profesional del agente.",
    )


def render_profiles_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    p = _profiles_map(ctx)
    chain = mapping_get_num(p, "chain", 0)
    agent_uri = mapping_get_num(p, "agent_uri", 0)

    if chain >= 1 and agent_uri >= 1:
        _verify_score("profiles", score, 2.5)
        return build_reason(
            "Core official sources (chain registration + URI) are present, "
            "establishing strong foundational identity and discoverability.",
            "Fuentes oficiales principales (registro en chain + URI) presentes, "
            "estableciendo una identidad fundamental sólida y descubribilidad.",
        )

    _verify_score("profiles", score, 0)
    return build_reason(
        "Missing essential basic sources (chain or URI), weakening the agent’s "
        "official identity and traceability.",
        "Faltan fuentes básicas esenciales (chain o URI), debilitando la "
        "identidad oficial y trazabilidad del agente.",
    )


def render_extended_profiles_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    ext_count = _ext_sources_count(ctx)
    if ext_count >= 1:
        expected = float(to_decimal(ext_count) * Decimal("0.75"))
        _verify_score("extended_profiles", score, expected)
        return build_reason(
            "Multiple external sources detected, enhancing the agent’s credibility "
            "and external validation.",
            "Múltiples fuentes externas detectadas, mejorando la credibilidad y "
            "validación externa del agente.",
            ext_sources_count=ext_count,
        )

    _verify_score("extended_profiles", score, 0)
    return build_reason(
        "No external sources found, limiting external validation and trust signals.",
        "No se encontraron fuentes externas, limitando la validación externa y "
        "las señales de confianza.",
    )


def render_basic_contact_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    # Text branches on persisted score (SQL CASE WHEN v_web_email_score > 0).
    has_web_email = score_gt(score, 0)
    if has_web_email:
        eng = (
            "Contact endpoints (web/email) present, enabling direct business "
            "communication and increasing accessibility."
        )
        esp = (
            "Endpoints de contacto (web/email) presentes, permitiendo comunicación "
            "directa de negocio y aumentando la accesibilidad."
        )
    else:
        eng = (
            "No contact endpoints (web/email) found, reducing accessibility and "
            "business trust."
        )
        esp = (
            "No se encontraron endpoints de contacto (web/email), reduciendo la "
            "accesibilidad y confianza empresarial."
        )
    return build_reason(eng, esp, has_web_email=has_web_email)


def render_extended_contact_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    has_programmatic = score_gt(score, 0)
    if has_programmatic:
        eng = (
            "Programmatic/API endpoints present, indicating advanced integration "
            "capability and developer-friendly design."
        )
        esp = (
            "Endpoints Programmatic/API presentes, indicando capacidad avanzada de "
            "integración y diseño amigable para desarrolladores."
        )
    else:
        eng = (
            "No programmatic endpoints found, limiting technical integration potential."
        )
        esp = (
            "No se encontraron endpoints programáticos, limitando el potencial de "
            "integración técnica."
        )
    return build_reason(eng, esp, has_programmatic=has_programmatic)


def render_supported_trust_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    has_supported_trust = score_gt(score, 0)
    if has_supported_trust:
        eng = (
            "Explicit supported trust declarations present, strengthening perceived "
            "reliability and ecosystem alignment."
        )
        esp = (
            "Declaraciones explícitas de trust soportado presentes, fortaleciendo la "
            "confiabilidad percibida y alineación con el ecosistema."
        )
    else:
        eng = (
            "No supported trust declarations found, missing an important trust signal."
        )
        esp = (
            "No se encontraron declaraciones de trust soportado, faltando una señal "
            "importante de confianza."
        )
    return build_reason(eng, esp, has_supported_trust=has_supported_trust)


def render_verification_methods_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    has_verification = score_gt(score, 0)
    if has_verification:
        eng = (
            "Verification methods present, providing strong proof of authenticity and "
            "increasing overall agent credibility."
        )
        esp = (
            "Métodos de verificación presentes, proporcionando fuerte prueba de "
            "autenticidad y aumentando la credibilidad general del agente."
        )
    else:
        eng = (
            "No verification methods found, reducing confidence in the agent’s "
            "authenticity."
        )
        esp = (
            "No se encontraron métodos de verificación, reduciendo la confianza en "
            "la autenticidad del agente."
        )
    return build_reason(eng, esp, has_verification=has_verification)


def render_basic_technical_metadata_reason(
    ctx: dict[str, Any], score: Any
) -> dict[str, Any]:
    fields_count = _tech_fields_count(ctx)
    if fields_count >= 2:
        expected = 1.5
        eng = (
            "Rich basic technical metadata across multiple fields, demonstrating "
            "clear technical capabilities."
        )
        esp = (
            "Metadatos técnicos básicos ricos en múltiples campos, demostrando "
            "capacidades técnicas claras."
        )
    elif fields_count == 1:
        expected = 1.0
        eng = (
            "Basic technical metadata present in one field, showing initial "
            "technical definition."
        )
        esp = (
            "Metadatos técnicos básicos presentes en un campo, mostrando definición "
            "técnica inicial."
        )
    else:
        expected = 0.0
        eng = (
            "Minimal or no technical metadata, limiting understanding of the "
            "agent’s technical capabilities."
        )
        esp = (
            "Metadatos técnicos mínimos o nulos, limitando la comprensión de las "
            "capacidades técnicas del agente."
        )
    _verify_score("basic_technical_metadata", score, expected)
    return build_reason(eng, esp, fields_count=fields_count)


def render_mcp_endpoint_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    has_mcp = score_gt(score, 0)
    if has_mcp:
        eng = (
            "MCP endpoint present, enabling advanced machine-to-machine communication "
            "and modern protocol integration."
        )
        esp = (
            "Endpoint MCP presente, permitiendo comunicación máquina-a-máquina "
            "avanzada e integración de protocolos modernos."
        )
    else:
        eng = "No MCP endpoint found, missing advanced communication capability."
        esp = (
            "No se encontró endpoint MCP, faltando capacidad de comunicación avanzada."
        )
    return build_reason(eng, esp, has_mcp=has_mcp)


def render_a2a_endpoint_reason(ctx: dict[str, Any], score: Any) -> dict[str, Any]:
    has_a2a = score_gt(score, 0)
    if has_a2a:
        eng = (
            "A2A endpoint present, supporting advanced agent-to-agent collaboration "
            "and ecosystem interoperability."
        )
        esp = (
            "Endpoint A2A presente, soportando colaboración agente-a-agente avanzada "
            "e interoperabilidad en el ecosistema."
        )
    else:
        eng = "No A2A endpoint found, limiting agent-to-agent interaction potential."
        esp = (
            "No se encontró endpoint A2A, limitando el potencial de interacción "
            "agente-a-agente."
        )
    return build_reason(eng, esp, has_a2a=has_a2a)


def render_advanced_technical_metadata_reason(
    ctx: dict[str, Any], score: Any
) -> dict[str, Any]:
    adv_count = _advanced_tech_count(ctx)
    if adv_count >= 2:
        expected = 2.0
        eng = (
            "Advanced technical setup with multiple professional components "
            "(tech stack, x402, tools, capabilities), indicating high maturity."
        )
        esp = (
            "Configuración técnica avanzada con múltiples componentes profesionales "
            "(tech stack, x402, tools, capabilities), indicando alta madurez."
        )
    else:
        expected = 0
        eng = (
            "Limited advanced technical configuration, reducing perceived "
            "sophistication."
        )
        esp = (
            "Configuración técnica avanzada limitada, reduciendo la sofisticación "
            "percibida."
        )
    _verify_score("advanced_technical_metadata", score, expected)
    return build_reason(eng, esp, advanced_tech_count=adv_count)


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
    for item in (*INFORMATION.basic, *INFORMATION.intermediate, *INFORMATION.advanced):
        total += to_decimal(scores.get(item.score_column), 0)
    return total


def render_information_pillar_summary(
    ctx: dict[str, Any], scores: dict[str, Any]
) -> dict[str, Any]:
    total = _pillar_total(scores)
    name_score = scores.get("name_score")
    description_score = scores.get("description_score")
    image_score = scores.get("image_score")
    extended_profiles_score = scores.get("extended_profiles_score")
    verification_score = scores.get("verification_methods_score")
    mcp_score = scores.get("mcp_endpoint_score")
    a2a_score = scores.get("a2a_endpoint_score")

    if score_ge(total, 22.0):
        overall_eng = (
            "The agent demonstrates excellent information maturity with strong, "
            "professional and verifiable identity."
        )
        overall_esp = (
            "El agente demuestra una excelente madurez de información con una "
            "identidad profesional fuerte y verificable."
        )
    elif score_ge(total, 18.5):
        overall_eng = (
            "The agent has solid information foundations with good external presence "
            "and technical definition."
        )
        overall_esp = (
            "El agente tiene fundamentos sólidos de información con buena presencia "
            "externa y definición técnica."
        )
    elif score_ge(total, 15.0):
        overall_eng = (
            "The agent has acceptable information quality but shows clear gaps in "
            "communication and verification."
        )
        overall_esp = (
            "El agente tiene calidad de información aceptable pero presenta brechas "
            "claras en comunicación y verificación."
        )
    elif score_ge(total, 11.0):
        overall_eng = (
            "The agent has weak information profile. Several key identity and "
            "discoverability elements are missing."
        )
        overall_esp = (
            "El agente tiene un perfil de información débil. Faltan varios elementos "
            "clave de identidad y descubribilidad."
        )
    else:
        overall_eng = (
            "The agent exhibits very weak information quality. Fundamental identity "
            "and credibility signals are severely lacking."
        )
        overall_esp = (
            "El agente presenta una calidad de información muy débil. Las señales "
            "fundamentales de identidad y credibilidad están severamente ausentes."
        )

    if score_eq(name_score, 0) or score_eq(description_score, 0):
        mid_eng = (
            "Critical gaps in basic identity (name and description) severely limit "
            "professional perception. "
        )
        mid_esp = (
            "Brechas críticas en identidad básica (nombre y descripción) limitan "
            "severamente la percepción profesional. "
        )
    elif score_eq(image_score, 0):
        mid_eng = "Missing professional image reduces visual credibility. "
        mid_esp = "Falta de imagen profesional reduce la credibilidad visual. "
    elif score_eq(extended_profiles_score, 0):
        mid_eng = "Lack of external sources weakens third-party validation. "
        mid_esp = "Falta de fuentes externas debilita la validación de terceros. "
    elif score_eq(verification_score, 0):
        mid_eng = "Absence of verification methods reduces trust. "
        mid_esp = "Ausencia de métodos de verificación reduce la confianza. "
    else:
        mid_eng = "The agent maintains reasonable information completeness. "
        mid_esp = "El agente mantiene una completitud de información razonable. "

    if score_lt(total, 11.0):
        tail_eng = "high risk of being perceived as unprofessional or incomplete."
        tail_esp = "alto riesgo de ser percibido como poco profesional o incompleto."
    elif score_lt(total, 15.0):
        tail_eng = "moderate risk with limited discoverability."
        tail_esp = "riesgo moderado con descubribilidad limitada."
    elif score_lt(total, 18.5):
        tail_eng = "acceptable but not competitive presentation."
        tail_esp = "presentación aceptable pero no competitiva."
    else:
        tail_eng = "strong market presence and high credibility potential."
        tail_esp = "fuerte presencia en el mercado y alto potencial de credibilidad."

    business_eng = (
        "This Information pillar assesses how well the agent presents itself to the "
        "world — its name, description, visual identity, external sources, and "
        "technical accessibility. "
        + mid_eng
        + "Overall, the current level suggests "
        + tail_eng
    )
    business_esp = (
        "Este pilar Information evalúa qué tan bien se presenta el agente al mundo — "
        "su nombre, descripción, identidad visual, fuentes externas y accesibilidad "
        "técnica. "
        + mid_esp
        + "En general, el nivel actual sugiere "
        + tail_esp
    )

    strengths_eng: list[str] = []
    strengths_esp: list[str] = []
    if score_eq(name_score, 3.0):
        strengths_eng.append("Professional and clear name")
        strengths_esp.append("Nombre profesional y claro")
    if score_ge(description_score, 1.8):
        strengths_eng.append("Rich and meaningful description")
        strengths_esp.append("Descripción rica y significativa")
    if score_gt(mcp_score, 0) or score_gt(a2a_score, 0):
        strengths_eng.append(
            "Advanced technical integration capabilities (MCP/A2A)"
        )
        strengths_esp.append(
            "Capacidades avanzadas de integración técnica (MCP/A2A)"
        )
    if score_gt(verification_score, 0):
        strengths_eng.append("Verification methods present")
        strengths_esp.append("Métodos de verificación presentes")

    concerns_eng: list[str] = []
    concerns_esp: list[str] = []
    if score_eq(name_score, 0):
        concerns_eng.append("Missing or placeholder name")
        concerns_esp.append("Nombre ausente o placeholder")
    if score_eq(description_score, 0):
        concerns_eng.append("Insufficient or empty description")
        concerns_esp.append("Descripción insuficiente o vacía")
    if score_eq(image_score, 0):
        concerns_eng.append("No professional image")
        concerns_esp.append("Sin imagen profesional")
    if score_eq(extended_profiles_score, 0):
        concerns_eng.append("Lack of external validation sources")
        concerns_esp.append("Falta de fuentes de validación externa")

    if score_eq(name_score, 0) or score_eq(description_score, 0):
        rec_eng = (
            "Priority: Define a clear, professional name and write a comprehensive "
            "description that communicates the agent’s value proposition."
        )
        rec_esp = (
            "Prioridad: Definir un nombre claro y profesional y escribir una "
            "descripción completa que comunique la propuesta de valor del agente."
        )
    elif score_eq(image_score, 0):
        rec_eng = (
            "Add a high-quality professional image to significantly improve visual "
            "identity and first impression."
        )
        rec_esp = (
            "Agregar una imagen profesional de alta calidad para mejorar "
            "significativamente la identidad visual y la primera impresión."
        )
    elif score_eq(extended_profiles_score, 0):
        rec_eng = (
            "Incorporate external sources (URI, DID, feedback) to strengthen "
            "credibility through third-party validation."
        )
        rec_esp = (
            "Incorporar fuentes externas (URI, DID, feedback) para fortalecer la "
            "credibilidad mediante validación de terceros."
        )
    elif score_eq(verification_score, 0):
        rec_eng = (
            "Implement verification methods to increase trust and authenticity signals."
        )
        rec_esp = (
            "Implementar métodos de verificación para aumentar las señales de "
            "confianza y autenticidad."
        )
    else:
        rec_eng = (
            "Continue enhancing advanced technical endpoints (MCP/A2A) and technical "
            "metadata to reach elite information maturity."
        )
        rec_esp = (
            "Continuar mejorando endpoints técnicos avanzados (MCP/A2A) y metadatos "
            "técnicos para alcanzar madurez de información de élite."
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
    "name_reason": render_name_reason,
    "description_reason": render_description_reason,
    "image_reason": render_image_reason,
    "profiles_reason": render_profiles_reason,
    "extended_profiles_reason": render_extended_profiles_reason,
    "basic_contact_reason": render_basic_contact_reason,
    "extended_contact_reason": render_extended_contact_reason,
    "supported_trust_reason": render_supported_trust_reason,
    "verification_methods_reason": render_verification_methods_reason,
    "basic_technical_metadata_reason": render_basic_technical_metadata_reason,
    "mcp_endpoint_reason": render_mcp_endpoint_reason,
    "a2a_endpoint_reason": render_a2a_endpoint_reason,
    "advanced_technical_metadata_reason": render_advanced_technical_metadata_reason,
}


def render_information_reasons(ctx: dict[str, Any], scores: dict[str, Any]) -> dict[str, Any]:
    """Devuelve {reason_column: reason_dict, ..., 'pillar_summary': dict}."""
    out: dict[str, Any] = {}
    for item in (*INFORMATION.basic, *INFORMATION.intermediate, *INFORMATION.advanced):
        renderer = _ITEM_RENDERERS[item.reason_column]
        out[item.reason_column] = renderer(ctx, scores.get(item.score_column))
    out["pillar_summary"] = render_information_pillar_summary(ctx, scores)
    return out


render_pillar_information = render_information_reasons
