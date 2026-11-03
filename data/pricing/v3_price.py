"""Uniswap v3 price conversion: sqrtPriceX96 -> human price.

A v3 pool stores sqrt(P) as a Q64.96 fixed-point number, where P is the price of token0 in
units of token1, both in *raw* (smallest-unit) amounts:

    P_raw = (sqrtPriceX96 / 2**96) ** 2          # raw token1 per raw token0

Raw units differ by token decimals, so the human price of token0 in token1 is

    P_human = P_raw * 10**(decimals0 - decimals1)

Which token is token0 is fixed by address order (token0 has the lower address). For the
Arbitrum WETH/USDC pools the registry shows token0 = WETH (18 decimals) and token1 = USDC
(6 decimals), so USDC per WETH = P_raw * 1e12. If a pool had the opposite order, the human
price would be inverted. `quote_per_base` handles both orders from the registry, instead of
hard-coding them.

Functions returning `Fraction` are exact (no rounding). `*_float` helpers round to IEEE-754
double (about 16 significant digits), which is fine for analytics but is an approximation.
"""

from __future__ import annotations

from fractions import Fraction

import polars as pl

Q96 = 2**96
Q192 = 2**192


def raw_price_token1_per_token0(sqrt_price_x96: int) -> Fraction:
    """Exact raw price: smallest units of token1 per smallest unit of token0."""
    if sqrt_price_x96 <= 0:
        raise ValueError("sqrtPriceX96 must be positive")
    return Fraction(sqrt_price_x96 * sqrt_price_x96, Q192)


def price_token1_per_token0(sqrt_price_x96: int, decimals0: int, decimals1: int) -> Fraction:
    """Exact human price: whole token1 per whole token0."""
    return raw_price_token1_per_token0(sqrt_price_x96) * Fraction(10**decimals0, 10**decimals1)


def quote_per_base(sqrt_price_x96: int, pool: dict, base: str, quote: str) -> Fraction:
    """Exact price of 1 `base` token in `quote` tokens, for a pool entry from configs/pools.yaml.
    `base` and `quote` are token addresses; token order is taken from the pool, not assumed."""
    t0, t1 = pool["token0"], pool["token1"]
    p = price_token1_per_token0(sqrt_price_x96, t0["decimals"], t1["decimals"])
    a0, a1, b, q = t0["address"].lower(), t1["address"].lower(), base.lower(), quote.lower()
    if (a0, a1) == (b, q):
        return p
    if (a0, a1) == (q, b):
        return 1 / p
    raise ValueError(f"pool {pool['address']} is not a {base}/{quote} pool")


def usdc_per_weth(sqrt_price_x96: int, pool: dict, registry: dict) -> Fraction:
    return quote_per_base(
        sqrt_price_x96, pool, registry["tokens"]["WETH"]["address"], registry["tokens"]["USDC"]["address"]
    )


def with_usdc_per_weth(swaps: pl.DataFrame, pool: dict, registry: dict) -> pl.DataFrame:
    """Add a float `usdc_per_weth` column to a swaps frame from data/collectors/swaps.py.
    Each value is computed exactly, then rounded once to float64."""
    prices = [float(usdc_per_weth(int(s), pool, registry)) for s in swaps["sqrt_price_x96"]]
    return swaps.with_columns(pl.Series("usdc_per_weth", prices, dtype=pl.Float64))
