"""Offline tests for data/pricing/v3_price.py."""

from fractions import Fraction

import pytest

from data.pricing.v3_price import (
    Q96,
    price_token1_per_token0,
    quote_per_base,
    raw_price_token1_per_token0,
)

WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"


def pool(t0, d0, t1, d1):
    return {"address": "0xpool", "token0": {"address": t0, "decimals": d0}, "token1": {"address": t1, "decimals": d1}}


WETH_USDC = pool(WETH, 18, USDC, 6)  # the real order on Arbitrum (WETH has the lower address)


def test_hand_checked_conversion():
    # Choose sqrtPriceX96 = 2**82, so sqrt(P_raw) = 2**82 / 2**96 = 2**-14 exactly.
    #   P_raw   = (2**-14)**2           = 2**-28 raw USDC per raw WETH
    #   P_human = 2**-28 * 10**(18 - 6) = 10**12 / 268_435_456
    #           = 3725.2902984619140625 USDC per WETH   (exact: 2**28 divides 10**12 * 10**16)
    p = quote_per_base(2**82, WETH_USDC, WETH, USDC)
    assert p == Fraction(10**12, 2**28)
    assert p == Fraction("3725.2902984619140625")


def test_realistic_price_round_trip():
    # sqrtPriceX96 for 2500 USDC/WETH: sqrt(2500e6 / 1e18) * 2**96 = 5e-5 * 2**96.
    # 2**96 * 5 / 100000 is not an integer, so floor it; the result is 2500 within ~1e-24.
    s = Q96 * 5 // 100_000
    p = quote_per_base(s, WETH_USDC, WETH, USDC)
    assert abs(p - 2500) < Fraction(1, 10**20)


def test_token_order_is_taken_from_pool():
    # Same sqrtPrice, hypothetical pool with USDC as token0: raw price is WETH-wei per USDC-unit,
    # human price is WETH per USDC = 2**-28 * 10**(6-18), and USDC per WETH is its inverse.
    flipped = pool(USDC, 6, WETH, 18)
    weth_per_usdc = price_token1_per_token0(2**82, 6, 18)
    assert weth_per_usdc == Fraction(1, 2**28 * 10**12)
    assert quote_per_base(2**82, flipped, WETH, USDC) == 2**28 * 10**12
    assert quote_per_base(2**82, flipped, USDC, WETH) == weth_per_usdc


def test_addresses_are_case_insensitive_and_checked():
    assert quote_per_base(2**82, WETH_USDC, WETH.lower(), USDC.upper().replace("0X", "0x")) == Fraction(10**12, 2**28)
    with pytest.raises(ValueError):
        quote_per_base(2**82, WETH_USDC, WETH, "0x0000000000000000000000000000000000000001")


def test_rejects_non_positive():
    with pytest.raises(ValueError):
        raw_price_token1_per_token0(0)
