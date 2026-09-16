"""Uniswap V3 liquidity ↔ token amounts (integer TickMath approximation)."""

from __future__ import annotations

from decimal import Decimal, getcontext

getcontext().prec = 80

Q96 = 1 << 96


def mul_div(a: int, b: int, denominator: int) -> int:
    if denominator == 0:
        return 0
    return (a * b) // denominator


def get_sqrt_ratio_at_tick(tick: int) -> int:
    """Approximate Uniswap TickMath.getSqrtRatioAtTick via Decimal."""
    return int(Decimal("1.0001") ** (Decimal(tick) / Decimal(2)) * Decimal(Q96))


def get_amount0_delta(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    if sqrt_a <= 0 or sqrt_b <= 0 or liquidity <= 0:
        return 0
    numerator1 = liquidity << 96
    numerator2 = sqrt_b - sqrt_a
    return mul_div(numerator1, numerator2, sqrt_b) // sqrt_a


def get_amount1_delta(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    if liquidity <= 0:
        return 0
    return mul_div(liquidity, sqrt_b - sqrt_a, Q96)


def amounts_for_liquidity(
    sqrt_price_x96: int,
    tick_lower: int,
    tick_upper: int,
    liquidity: int,
) -> tuple[int, int]:
    if liquidity <= 0:
        return 0, 0
    sqrt_a = get_sqrt_ratio_at_tick(tick_lower)
    sqrt_b = get_sqrt_ratio_at_tick(tick_upper)
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a

    if sqrt_price_x96 <= sqrt_a:
        return get_amount0_delta(sqrt_a, sqrt_b, liquidity), 0
    if sqrt_price_x96 < sqrt_b:
        return (
            get_amount0_delta(sqrt_price_x96, sqrt_b, liquidity),
            get_amount1_delta(sqrt_a, sqrt_price_x96, liquidity),
        )
    return 0, get_amount1_delta(sqrt_a, sqrt_b, liquidity)
