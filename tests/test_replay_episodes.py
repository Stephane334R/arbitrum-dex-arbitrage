"""Tests for research/replay/replay.py: episode merging, durations, censoring, sizing."""

import math

import polars as pl
import pytest

from research.replay.replay import (
    PoolMeta,
    _virtual_reserves,
    build_segments,
    exact_breakeven_bps,
    find_episodes,
    optimal_arb,
)

WINDOW = (100, 199)


def seg_rows(pair, spec):
    """spec: list of (block_start, block_end, ts_start, above, gross)."""
    return [
        {"pair": pair, "block_start": a, "block_end": b, "ts_start": ts, "above": ab,
         "gap_bps": 10.0 + g, "gross_usdc": g}
        for a, b, ts, ab, g in spec
    ]


def episodes(spec_by_pair, end_ts=None):
    rows = [r for pair, spec in spec_by_pair.items() for r in seg_rows(pair, spec)]
    ep = find_episodes(pl.DataFrame(rows), WINDOW, window_end_close_ts=end_ts)
    return ep if ep.is_empty() else ep.sort(["pair", "start_block"])


def test_consecutive_above_segments_merge_into_one_episode():
    ep = episodes({"x": [(100, 119, 0, False, 0), (120, 124, 5, True, 1.0), (125, 129, 6, True, 3.0),
                         (130, 199, 8, False, 0)]})
    assert ep.height == 1
    e = ep.row(0, named=True)
    assert (e["start_block"], e["end_block"], e["close_block"]) == (120, 129, 130)
    assert e["duration_blocks"] == 10
    assert e["duration_s"] == 8 - 5  # ts(close block) - ts(start)
    assert e["peak_gross_usdc"] == 3.0 and e["peak_block"] == 125 and e["peak_gap_bps"] == 13.0
    assert not e["left_censored"] and not e["right_censored"]


def test_a_below_segment_splits_episodes():
    ep = episodes({"x": [(100, 109, 0, False, 0), (110, 110, 1, True, 1.0), (111, 111, 1, False, 0),
                         (112, 112, 2, True, 2.0), (113, 199, 3, False, 0)]})
    assert ep["start_block"].to_list() == [110, 112]
    assert ep["duration_blocks"].to_list() == [1, 1]
    assert ep["duration_s"].to_list() == [0, 1]  # same-second blocks give 0 s


def test_pairs_are_independent():
    ep = episodes({"a": [(100, 149, 0, False, 0), (150, 199, 5, True, 1.0)],
                   "b": [(100, 149, 0, True, 2.0), (150, 199, 5, False, 0)]}, end_ts=9)
    a = ep.filter(pl.col("pair") == "a").row(0, named=True)
    b = ep.filter(pl.col("pair") == "b").row(0, named=True)
    assert a["right_censored"] and not a["left_censored"] and a["duration_s"] == 9 - 5
    assert b["left_censored"] and not b["right_censored"] and b["duration_blocks"] == 50


def test_no_episodes():
    assert episodes({"x": [(100, 199, 0, False, 0)]}).is_empty()


def test_build_segments_forward_fills_and_seeds_initial_state():
    def sw(block, log_index, sqrt, ts):
        return {"block_number": block, "log_index": log_index, "sqrt_price_x96": str(sqrt), "liquidity": "7",
                "tick": 1, "block_timestamp": ts}

    a = pl.DataFrame([sw(105, 0, 11, 50), sw(105, 3, 12, 50), sw(150, 1, 13, 60)])
    b = pl.DataFrame([sw(120, 2, 21, 55)])
    init = {"A": {"sqrt_price_x96": 10, "liquidity": 7, "tick": 0}, "B": {"sqrt_price_x96": 20, "liquidity": 7, "tick": 0}}
    seg = build_segments({"A": a, "B": b}, init, WINDOW, window_start_ts=40)
    assert seg["block_start"].to_list() == [100, 105, 120, 150]
    assert seg["block_end"].to_list() == [104, 119, 149, 199]
    assert seg["sqrt_0"].to_list() == ["10", "12", "12", "13"]  # last swap in block 105 wins
    assert seg["sqrt_1"].to_list() == ["20", "20", "21", "21"]
    assert seg["ts_start"].to_list() == [40, 50, 55, 60]


def _meta(fee, spacing=10):
    return PoolMeta(address=f"p{fee}", fee=fee, tick_spacing=spacing, weth_is_token0=True, weth_decimals=18, usdc_decimals=6)


def _state(price_usdc_per_weth, liquidity):
    raw = price_usdc_per_weth * 1e6 / 1e18
    sqrt = math.sqrt(raw)
    return {"sqrt": int(sqrt * 2**96), "liq": liquidity, "tick": math.floor(math.log(raw) / math.log(1.0001))}


def test_optimal_arb_matches_brute_force():
    mc, me = _meta(100, 1), _meta(500)
    cheap, exp = _state(2000.0, 10**18), _state(2004.0, 10**18)  # 20 bps gap > 6 bps fees
    res = optimal_arb(cheap, exp, mc, me)
    wA, uA = _virtual_reserves(cheap["sqrt"], cheap["liq"], mc)
    wB, uB = _virtual_reserves(exp["sqrt"], exp["liq"], me)

    def profit(d):  # simulate the two constant-product swaps directly
        w = wA * mc.gamma * d / (uA + mc.gamma * d)
        return uB * me.gamma * w / (wB + me.gamma * w) - d

    d_star = res["size_usdc"] * 1e6
    best = max(profit(d_star * k / 100) for k in range(1, 300))
    assert res["gross_usdc"] > 0
    assert profit(d_star) == pytest.approx(res["gross_usdc"] * 1e6, rel=1e-9)
    assert profit(d_star) >= best * (1 - 1e-12)


def test_optimal_arb_zero_below_breakeven():
    res = optimal_arb(_state(2000.0, 10**18), _state(2001.0, 10**18), _meta(100, 1), _meta(500))  # 5 bps < 6.0006
    assert res["gross_usdc"] == 0.0 and res["size_usdc"] == 0.0


def test_exact_breakeven_slightly_above_sum_of_fees():
    assert 6 < exact_breakeven_bps(100, 500) < 6.01
