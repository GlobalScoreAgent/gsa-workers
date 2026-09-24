"""Helpers compartidos para renderizar reasons bilingües (paridad con SQL jsonb)."""

from __future__ import annotations

import json
from calendar import monthrange
from datetime import date, datetime, time, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any


def coalesce(value: Any, default: Any = 0) -> Any:
    return default if value is None else value


def to_decimal(value: Any, default: Decimal | int | float = 0) -> Decimal:
    if value is None:
        return Decimal(str(default))
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    return Decimal(str(value))


def sql_round(value: Any, ndigits: int = 2) -> float:
    """Equivalente práctico de round(numeric, n) de Postgres (half away from zero)."""
    quant = Decimal("1").scaleb(-ndigits)
    return float(to_decimal(value).quantize(quant, rounding=ROUND_HALF_UP))


def score_eq(score: Any, target: Any) -> bool:
    if score is None:
        return False
    return to_decimal(score) == to_decimal(target)


def score_gt(score: Any, target: Any) -> bool:
    if score is None:
        return False
    return to_decimal(score) > to_decimal(target)


def score_ge(score: Any, target: Any) -> bool:
    if score is None:
        return False
    return to_decimal(score) >= to_decimal(target)


def score_lt(score: Any, target: Any) -> bool:
    if score is None:
        return True
    return to_decimal(score) < to_decimal(target)


def build_reason(reason_eng: str, reason_esp: str, **metrics: Any) -> dict[str, Any]:
    """Orden como jsonb_build_object SQL: métricas primero, luego reason_eng / reason_esp."""
    out: dict[str, Any] = {}
    for key, value in metrics.items():
        out[key] = value
    out["reason_eng"] = reason_eng
    out["reason_esp"] = reason_esp
    return out


def as_mapping(value: Any) -> dict[str, Any] | None:
    """Replica jsonb_typeof(...) = 'object': dict (no list). None si no aplica."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def mapping_get_num(mapping: dict[str, Any] | None, key: str, default: Decimal | int = 0) -> Decimal:
    if not mapping:
        return to_decimal(default)
    return to_decimal(mapping.get(key), default)


def as_of_date(ctx: dict[str, Any]) -> date:
    """CURRENT_DATE del SQL; override con ctx['as_of'] para tests."""
    raw = ctx.get("as_of")
    if raw is None:
        return datetime.now(timezone.utc).date()
    parsed = as_date(raw)
    return parsed if parsed is not None else datetime.now(timezone.utc).date()


def as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).date()
        except ValueError:
            return date.fromisoformat(text[:10])
    return None


def as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def add_calendar_months(d: date, months: int) -> date:
    """Aritmética de meses de calendario (como INTERVAL 'N months' sobre date)."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)


def agent_age_months(on_chain_created_at: Any, as_of: date) -> int:
    """EXTRACT(EPOCH FROM (CURRENT_DATE - on_chain_created_at))::int / (30 * 86400)."""
    created = as_datetime(on_chain_created_at)
    if created is None:
        return 0
    as_of_ts = datetime.combine(as_of, time.min, tzinfo=timezone.utc)
    epoch_secs = int((as_of_ts - created).total_seconds())
    if epoch_secs < 0:
        return 0
    return epoch_secs // (30 * 86400)


def days_since(first_tx: Any, as_of: date) -> int:
    """EXTRACT(DAY FROM CURRENT_DATE - wallet_created_at)::int (intervalo en días)."""
    created = as_date(first_tx)
    if created is None:
        return 0
    return (as_of - created).days


def jsonb_agg(items: list[str]) -> list[str] | None:
    """jsonb_agg sobre filas filtradas: NULL si el conjunto queda vacío."""
    return items if items else None


def whole_number(value: Any) -> int | float:
    """Emite int cuando el numeric SQL es entero; si no, float."""
    d = to_decimal(value)
    if d == d.to_integral_value():
        return int(d)
    return float(d)
