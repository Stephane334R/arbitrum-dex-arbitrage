"""Offline tests for the swap collector: log decoding and cache range bookkeeping."""

from eth_abi import encode

from data.collectors.swaps import (
    SWAP_TOPIC,
    covered_ranges,
    decode_swap_log,
    load_swaps,
    missing_ranges,
    plan_fetches,
    write_cache_file,
)

POOL = "0xC6962004f452bE9203591991D15f6b388e09E8D0"


def make_log(block=100, log_index=3, amount0=-10**18, amount1=2_500 * 10**6, sqrtp=2**82, liq=10**20, tick=-12345):
    data = encode(["int256", "int256", "uint160", "uint128", "int24"], [amount0, amount1, sqrtp, liq, tick])
    return {
        "address": POOL.lower(),
        "topics": [SWAP_TOPIC, "0x" + "00" * 12 + "11" * 20, "0x" + "00" * 12 + "22" * 20],
        "data": "0x" + data.hex(),
        "blockNumber": hex(block),
        "blockTimestamp": hex(1_700_000_000),
        "transactionHash": "0x" + "ab" * 32,
        "logIndex": hex(log_index),
        "removed": False,
    }


def test_swap_topic():
    assert SWAP_TOPIC == "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"


def test_decode_swap_log_signed_and_large_values():
    row = decode_swap_log(make_log())
    assert row["pool"] == POOL
    assert row["block_number"] == 100 and row["log_index"] == 3
    assert row["block_timestamp"] == 1_700_000_000
    assert row["amount0"] == str(-10**18) and row["amount1"] == str(2_500 * 10**6)
    assert row["sqrt_price_x96"] == str(2**82) and row["liquidity"] == str(10**20)
    assert row["tick"] == -12345
    assert row["sender"].lower() == "0x" + "11" * 20


def test_missing_ranges():
    assert missing_ranges((1, 100), []) == [(1, 100)]
    assert missing_ranges((1, 100), [(1, 100)]) == []
    assert missing_ranges((1, 100), [(10, 19), (30, 200)]) == [(1, 9), (20, 29)]
    assert missing_ranges((50, 60), [(1, 49), (61, 70)]) == [(50, 60)]
    assert missing_ranges((1, 10), [(1, 5), (3, 10)]) == []


def test_plan_fetches_only_requests_missing_pools():
    gaps = {"A": [(1, 25)], "B": [(11, 25)]}
    chunks = plan_fetches(gaps, max_block_range=10)
    assert chunks == [(1, 10, ("A",)), (11, 20, ("A", "B")), (21, 25, ("A", "B"))]


def test_cache_roundtrip_and_index(tmp_path):
    pool_dir = tmp_path / POOL.lower()
    write_cache_file(pool_dir, (100, 199), [decode_swap_log(make_log(block=150))])
    write_cache_file(pool_dir, (200, 299), [])  # empty ranges are cached too
    assert covered_ranges(pool_dir) == [(100, 199), (200, 299)]
    assert missing_ranges((100, 350), covered_ranges(pool_dir)) == [(300, 350)]
    df = load_swaps(POOL, cache_root=tmp_path)
    assert df.height == 1 and df["sqrt_price_x96"][0] == str(2**82)
