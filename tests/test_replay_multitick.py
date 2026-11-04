"""Tests for research/replay/multitick.py on synthetic pools (WETH = token0, USDC = token1)."""

import pytest

from data.pricing.v3_swap import get_sqrt_ratio_at_tick, snapshot_from_positions
from research.replay.multitick import optimal_arb_exact, round_trip
from research.replay.replay import PoolMeta, optimal_arb

# Raw USDC per raw WETH ~ 1.0001^tick. Tick -195000 gives ~3,400 USDC/WETH at 18/6 decimals,
# so sizes and profits are of realistic magnitude.
BASE_TICK = -195000
L_DEEP = 10**19


def meta(fee, spacing):
    return PoolMeta(address=f"pool{fee}", fee=fee, tick_spacing=spacing, weth_is_token0=True,
                    weth_decimals=18, usdc_decimals=6)


def pool(fee, spacing, tick, positions):
    return snapshot_from_positions(get_sqrt_ratio_at_tick(tick) + 1, fee, spacing, positions, word_range=8)


def wide(spacing, liquidity=L_DEEP):
    lo = (BASE_TICK - 6000) // spacing * spacing
    return [(lo, lo + 12000 // spacing * spacing, liquidity)]


def single_tick(cheap, exp, mc, me):
    st = lambda s: {"sqrt": str(s.sqrt_price_x96), "liq": str(s.liquidity), "tick": s.tick}
    return optimal_arb(st(cheap), st(exp), mc, me)


def test_matches_single_tick_closed_form_when_no_tick_is_crossed():
    mc, me = meta(100, 1), meta(500, 10)
    cheap = pool(100, 1, BASE_TICK, wide(1))
    exp = pool(500, 10, BASE_TICK + 20, wide(10))  # ~20 bps gap > 6 bps of fees
    r = optimal_arb_exact(cheap, exp, True, True)
    st = single_tick(cheap, exp, mc, me)
    assert r["trip"].leg1.ticks_crossed == [] and r["trip"].leg2.ticks_crossed == []
    assert st["within_tick_range"] is not None
    assert r["profit_raw"] / 1e6 == pytest.approx(st["gross_usdc"], rel=1e-4)
    assert r["size_raw"] / 1e6 == pytest.approx(st["size_usdc"], rel=1e-2)  # profit is flat near the optimum


def test_lower_than_single_tick_when_liquidity_thins_out():
    mc, me = meta(100, 1), meta(500, 10)
    # Deep liquidity only in a narrow band just above the cheap pool's price, thin beyond it.
    cheap_pos = [(BASE_TICK - 10, BASE_TICK + 3, L_DEEP)] + wide(1, L_DEEP // 100)
    cheap = pool(100, 1, BASE_TICK, cheap_pos)
    exp = pool(500, 10, BASE_TICK + 20, wide(10))
    r = optimal_arb_exact(cheap, exp, True, True)
    st = single_tick(cheap, exp, mc, me)
    assert r["trip"].leg1.ticks_crossed  # the optimal trade does cross into the thin range
    assert 0 < r["profit_raw"] / 1e6 < st["gross_usdc"]


def test_no_gap_means_no_trade():
    cheap = pool(100, 1, BASE_TICK, wide(1))
    exp = pool(500, 10, BASE_TICK + 2, wide(10))  # 2 bps gap < 6 bps fees
    r = optimal_arb_exact(cheap, exp, True, True)
    assert r["size_raw"] == 0 and r["profit_raw"] == 0 and r["trip"] is None


def test_search_finds_the_optimum():
    cheap = pool(100, 1, BASE_TICK, [(BASE_TICK - 10, BASE_TICK + 3, L_DEEP)] + wide(1, L_DEEP // 100))
    exp = pool(500, 10, BASE_TICK + 20, wide(10))
    r = optimal_arb_exact(cheap, exp, True, True)
    d = r["size_raw"]
    for f in (0.5, 0.9, 0.99, 0.999, 1.001, 1.01, 1.1, 2.0):
        rt = round_trip(cheap, exp, True, True, int(d * f))
        assert rt.profit <= r["profit_raw"]
