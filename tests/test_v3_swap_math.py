"""Offline unit tests for data/pricing/v3_swap.py.

These check the port against independent references (80-digit Decimal math, brute force,
closed-form single-range swaps) and against invariants that the Solidity code guarantees.
Bit-exact agreement with the deployed contracts is checked separately, against QuoterV2, in
tests/test_v3_swap_parity.py.
"""

import random
from decimal import Decimal, getcontext
from fractions import Fraction

import pytest

from data.pricing.v3_swap import (
    MAX_SQRT_RATIO,
    MAX_TICK,
    MAX_UINT160,
    MAX_UINT256,
    MIN_SQRT_RATIO,
    MIN_TICK,
    Q96,
    SolidityRevert,
    TickDataOutOfRange,
    add_delta,
    compress,
    compute_swap_step,
    count_initialized_ticks_crossed,
    div_rounding_up,
    get_amount0_delta,
    get_amount1_delta,
    get_next_sqrt_price_from_amount0_rounding_up,
    get_next_sqrt_price_from_input,
    get_next_sqrt_price_from_output,
    get_sqrt_ratio_at_tick,
    get_tick_at_sqrt_ratio,
    mul_div,
    mul_div_rounding_up,
    snapshot_from_positions,
    swap,
)

getcontext().prec = 90
RNG = random.Random(42)


# --- TickMath ---------------------------------------------------------------------------------


def test_tick_math_bounds():
    assert get_sqrt_ratio_at_tick(MIN_TICK) == MIN_SQRT_RATIO
    assert get_sqrt_ratio_at_tick(MAX_TICK) == MAX_SQRT_RATIO
    assert get_sqrt_ratio_at_tick(0) == Q96
    assert get_tick_at_sqrt_ratio(MIN_SQRT_RATIO) == MIN_TICK
    assert get_tick_at_sqrt_ratio(MAX_SQRT_RATIO - 1) == MAX_TICK - 1
    for bad in (MIN_TICK - 1, MAX_TICK + 1):
        with pytest.raises(SolidityRevert):
            get_sqrt_ratio_at_tick(bad)
    for bad in (MIN_SQRT_RATIO - 1, MAX_SQRT_RATIO):
        with pytest.raises(SolidityRevert):
            get_tick_at_sqrt_ratio(bad)


@pytest.mark.parametrize("tick", [MIN_TICK, MAX_TICK, -1, 0, 1] + [RNG.randint(MIN_TICK, MAX_TICK) for _ in range(500)])
def test_sqrt_ratio_matches_high_precision(tick):
    # Independent reference: sqrt(1.0001^tick) * 2^96 at 90 significant digits. The on-chain
    # value is that, up to integer rounding (<= 1) plus a relative error measured at < 5e-20.
    exact = (Decimal("1.0001") ** tick).sqrt() * Decimal(2**96)
    got = get_sqrt_ratio_at_tick(tick)
    assert abs(Decimal(got) - exact) <= 1 + exact * Decimal("1e-18")


def test_tick_round_trip_and_boundaries():
    ticks = list(range(-1000, 1001)) + [RNG.randint(MIN_TICK + 1, MAX_TICK - 1) for _ in range(2000)]
    for t in ticks:
        s = get_sqrt_ratio_at_tick(t)
        assert get_tick_at_sqrt_ratio(s) == t
        assert get_tick_at_sqrt_ratio(s - 1) == t - 1  # greatest tick with ratio <= price
        assert get_sqrt_ratio_at_tick(t + 1) > s


# --- FullMath / UnsafeMath / LiquidityMath -----------------------------------------------------


def test_mul_div_exact_and_reverts():
    for _ in range(500):
        a, b, d = RNG.getrandbits(256), RNG.getrandbits(256), RNG.getrandbits(256) or 1
        if a * b // d > MAX_UINT256:
            with pytest.raises(SolidityRevert):
                mul_div(a, b, d)
            continue
        assert mul_div(a, b, d) == Fraction(a * b, d).__floor__()
        if a * b % d and a * b // d == MAX_UINT256:
            continue
        assert mul_div_rounding_up(a, b, d) == Fraction(a * b, d).__ceil__()
    with pytest.raises(SolidityRevert):
        mul_div(1, 1, 0)
    assert div_rounding_up(7, 2) == 4 and div_rounding_up(6, 2) == 3 and div_rounding_up(5, 0) == 0


def test_add_delta():
    assert add_delta(10, -3) == 7 and add_delta(10, 5) == 15
    with pytest.raises(SolidityRevert):
        add_delta(3, -4)  # LS
    with pytest.raises(SolidityRevert):
        add_delta(2**128 - 1, 1)  # LA


# --- SqrtPriceMath -----------------------------------------------------------------------------


def _rand_price_liq():
    return get_sqrt_ratio_at_tick(RNG.randint(-300000, 300000)), RNG.randint(10**6, 10**28)


def test_amount_deltas_against_exact():
    for _ in range(500):
        (sa, liq), sb = _rand_price_liq(), get_sqrt_ratio_at_tick(RNG.randint(-300000, 300000))
        lo, hi = min(sa, sb), max(sa, sb)
        exact0 = Fraction(liq * (hi - lo) * Q96, lo * hi)
        exact1 = Fraction(liq * (hi - lo), Q96)
        d0, u0 = get_amount0_delta(sa, sb, liq, False), get_amount0_delta(sa, sb, liq, True)
        d1, u1 = get_amount1_delta(sa, sb, liq, False), get_amount1_delta(sa, sb, liq, True)
        assert d0 <= exact0 <= u0 and u0 - d0 <= 2  # two-stage rounding: off by at most 1 each way
        assert d1 == exact1.__floor__() and u1 == exact1.__ceil__()
        assert get_amount0_delta(sb, sa, liq, True) == u0  # argument order does not matter


def test_next_price_from_input_never_overshoots():
    """Rounding favours the pool: the input needed to reach the computed next price is never
    more than the input supplied."""
    for _ in range(500):
        s, liq = _rand_price_liq()
        amt = RNG.randint(1, 10**24)
        n0 = get_next_sqrt_price_from_input(s, liq, amt, True)
        assert n0 <= s and get_amount0_delta(n0, s, liq, True) <= amt
        n1 = get_next_sqrt_price_from_input(s, liq, amt, False)
        assert n1 >= s and get_amount1_delta(s, n1, liq, True) <= amt


def test_next_price_from_output_always_reaches():
    for _ in range(500):
        s, liq = _rand_price_liq()
        amt = RNG.randint(1, 10**12)
        try:
            n = get_next_sqrt_price_from_output(s, liq, amt, True)  # token1 out
        except SolidityRevert:
            continue  # asked for more than the range holds
        assert n < s and get_amount1_delta(n, s, liq, False) >= amt


def test_amount0_add_overflow_fallback_branch():
    # amount * sqrtP overflows uint256, so Solidity takes the L / (L/sqrtP + amount) branch.
    s, liq, amt = get_sqrt_ratio_at_tick(0), 10**18, 2**200
    n1 = liq << 96
    assert amt * s > MAX_UINT256
    expected = div_rounding_up(n1, n1 // s + amt)
    assert get_next_sqrt_price_from_amount0_rounding_up(s, liq, amt, True) == expected


# --- SwapMath ----------------------------------------------------------------------------------


@pytest.mark.parametrize("fee", [100, 500, 3000, 10000])
def test_compute_swap_step_invariants(fee):
    for _ in range(300):
        cur, liq = _rand_price_liq()
        tgt = get_sqrt_ratio_at_tick(get_tick_at_sqrt_ratio(cur) + RNG.choice([-1, 1]) * RNG.randint(1, 2000))
        rem = RNG.randint(1, 10**24)
        nxt, a_in, a_out, f = compute_swap_step(cur, tgt, liq, rem, fee)
        z41 = cur >= tgt
        assert (tgt <= nxt <= cur) if z41 else (cur <= nxt <= tgt)
        assert a_in + f <= rem
        if nxt != tgt:
            assert a_in + f == rem  # stopped inside the range: the whole remainder is used
        # exact output with the amount just obtained never needs more input
        if a_out > 0:
            nxt2, a_in2, a_out2, f2 = compute_swap_step(cur, tgt, liq, -a_out, fee)
            assert a_out2 == a_out and a_in2 + f2 <= a_in + f


# --- TickBitmap + swap loop on synthetic pools -------------------------------------------------


def _brute_next(snap, tick, lte):
    """Reference: scan ticks one by one, stopping at the word boundary like TickBitmap."""
    c = compress(tick, snap.tick_spacing)
    init = {compress(t, snap.tick_spacing) for t in snap.liquidity_net}
    if lte:
        boundary = (c >> 8) << 8
        for x in range(c, boundary - 1, -1):
            if x in init:
                return x * snap.tick_spacing, True
        return boundary * snap.tick_spacing, False
    boundary = (((c + 1) >> 8) << 8) + 255
    for x in range(c + 1, boundary + 1):
        if x in init:
            return x * snap.tick_spacing, True
    return boundary * snap.tick_spacing, False


@pytest.mark.parametrize("spacing", [1, 10, 60])
def test_next_initialized_tick_matches_brute_force(spacing):
    positions = []
    for _ in range(40):
        a, b = sorted(RNG.sample(range(-1500, 1500), 2))
        positions.append((a * spacing, b * spacing, 10**18))
    snap = snapshot_from_positions(get_sqrt_ratio_at_tick(0), 500, spacing, positions, word_range=10)
    for _ in range(2000):
        t = RNG.randint(-1400 * spacing, 1400 * spacing)
        for lte in (True, False):
            assert snap.next_initialized_tick_within_one_word(t, lte) == _brute_next(snap, t, lte)


def _single_range_pool(fee=500, spacing=10):
    return snapshot_from_positions(get_sqrt_ratio_at_tick(5), fee, spacing, [(-1000, 1000, 10**21)])


@pytest.mark.parametrize("zero_for_one", [True, False])
def test_swap_inside_one_range_matches_closed_form(zero_for_one):
    pool = _single_range_pool()
    amount_in = 10**15
    res = swap(pool, zero_for_one, amount_in)
    s, liq = Fraction(pool.sqrt_price_x96, Q96), pool.liquidity
    x = Fraction(amount_in) * Fraction(10**6 - pool.fee, 10**6)
    if zero_for_one:  # 1/sqrtP' = 1/sqrtP + x/L ; out = L (sqrtP - sqrtP')
        s2 = 1 / (1 / s + x / liq)
        ideal_out = liq * (s - s2)
    else:  # sqrtP' = sqrtP + x/L ; out = L (1/sqrtP - 1/sqrtP')
        s2 = s + x / liq
        ideal_out = liq * (1 / s - 1 / s2)
    assert res.amount_in == amount_in
    assert ideal_out - 2 <= res.amount_out <= ideal_out  # rounding always favours the pool
    assert res.ticks_crossed == []
    # several word-boundary steps can happen even inside one range (spacing 10: word = 2560 ticks)
    assert res.steps >= 1


def test_swap_crosses_ticks_and_updates_liquidity():
    positions = [(-600, 600, 10**18), (-240, 240, 4 * 10**18), (-60, 900, 10**18)]
    # 60 words of 256*60 ticks reach MIN_TICK, so the swap can run out to the price limit
    pool = snapshot_from_positions(get_sqrt_ratio_at_tick(3), 3000, 60, positions, word_range=60)
    assert pool.liquidity == 6 * 10**18
    res = swap(pool, True, 10**20)  # sells token0 until the price limit or data runs out
    assert res.ticks_crossed == sorted(res.ticks_crossed, reverse=True)
    assert set(res.ticks_crossed) == {-60, -240, -600}
    assert res.liquidity_after == 0  # every position is above the final price
    assert res.amount_in < 10**20  # partial fill: hit the price limit with no liquidity left
    up = swap(pool, False, 10**17)
    assert up.sqrt_price_x96_after > pool.sqrt_price_x96 and up.amount0 < 0 < up.amount1


def test_swap_exact_output_round_trip():
    pool = _single_range_pool()
    ex_in = swap(pool, True, 10**16)
    ex_out = swap(pool, True, -ex_in.amount_out)
    assert ex_out.amount_out == ex_in.amount_out
    assert ex_out.amount_in <= ex_in.amount_in


def test_swap_refuses_to_guess_outside_fetched_words():
    pool = snapshot_from_positions(get_sqrt_ratio_at_tick(0), 500, 10, [(-10**5, 10**5, 10**18)], word_range=1)
    with pytest.raises(TickDataOutOfRange):
        swap(pool, True, 10**30)


def test_swap_rejects_bad_inputs():
    pool = _single_range_pool()
    with pytest.raises(SolidityRevert):
        swap(pool, True, 0)
    with pytest.raises(SolidityRevert):
        swap(pool, True, 10, sqrt_price_limit_x96=pool.sqrt_price_x96 + 1)


def test_quoter_tick_counter():
    positions = [(-600, 600, 10**18), (-200, 200, 10**18)]
    pool = snapshot_from_positions(get_sqrt_ratio_at_tick(3), 3000, 1, positions, word_range=4)
    assert count_initialized_ticks_crossed(pool, 3, -250) == 1  # crosses -200
    assert count_initialized_ticks_crossed(pool, 3, 650) == 2  # crosses 200 and 600
    assert count_initialized_ticks_crossed(pool, 3, 4) == 0
    # landing exactly on an initialized tick while moving down does not count it as crossed
    assert count_initialized_ticks_crossed(pool, 3, -200) == 0


def test_uint160_limit_constant():
    assert MAX_UINT160 == 2**160 - 1
