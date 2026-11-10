"""Offline tests for the swap collector: log decoding and cache range bookkeeping."""
import pytest

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


def test_resolve_head_accepts_a_finalized_block_number():
    from data.collectors.swaps import resolve_head

    class FakeClient:
        def call(self, method, params):
            assert method == "eth_getBlockByNumber"
            return {"number": hex(1000)}

    assert resolve_head(FakeClient(), "finalized") == 1000
    assert resolve_head(FakeClient(), 900) == 900
    assert resolve_head(FakeClient(), "900") == 900
    with pytest.raises(ValueError):
        resolve_head(FakeClient(), 1001)  # not finalized yet: could still be reorged
    with pytest.raises(ValueError):
        resolve_head(FakeClient(), "pending")


def test_collect_several_specs_one_request_per_chunk(tmp_path):
    """Swap + Mint/Burn in one pass: each chunk is one eth_getLogs with both topic sets, logs
    go to their own cache, and a range already cached for one spec is not written again."""
    from data.collectors.liquidity_events import BURN_TOPIC, LIQUIDITY_SPEC, MINT_TOPIC, load_liquidity_events
    from data.collectors.swaps import SWAP_SPEC, collect
    from dataclasses import replace

    liq_spec = replace(LIQUIDITY_SPEC, cache_root=tmp_path / "liq")
    swap_root = tmp_path / "swaps"
    write_cache_file(swap_root / POOL.lower(), (100, 109), [])  # swaps already cached for the first chunk

    mint_data = encode(["address", "uint128", "uint256", "uint256"], ["0x" + "44" * 20, 7, 1, 2])
    mint = {"address": POOL.lower(), "topics": [MINT_TOPIC, "0x" + "00" * 32, "0x" + encode(["int24"], [-10]).hex(),
                                                 "0x" + encode(["int24"], [10]).hex()],
            "data": "0x" + mint_data.hex(), "blockNumber": hex(105), "blockTimestamp": hex(1), "transactionHash": "0xaa",
            "transactionIndex": hex(3), "logIndex": hex(4)}
    swap_in_cached = {**make_log(block=106), "transactionIndex": hex(2)}  # must be dropped: (swap, 100..109) cached
    swap_new = {**make_log(block=112), "transactionIndex": hex(9)}
    calls = []

    class FakeClient:
        def call(self, method, params):
            q = params[0]
            calls.append((int(q["fromBlock"], 16), int(q["toBlock"], 16), q["topics"]))
            lo, hi = int(q["fromBlock"], 16), int(q["toBlock"], 16)
            return [l for l in (mint, swap_in_cached, swap_new) if lo <= int(l["blockNumber"], 16) <= hi]

    cfg = {"max_block_range": 10, "concurrency": 1, "flush_blocks": 1000}
    n = collect(FakeClient(), [POOL], (100, 119), cfg, cache_root=swap_root, specs=[SWAP_SPEC, liq_spec])
    assert n == 2
    assert calls[0] == (100, 109, [[MINT_TOPIC, BURN_TOPIC]])  # swaps cached: only Mint/Burn asked (OR-list)
    assert calls[1] == (110, 119, [[SWAP_TOPIC, MINT_TOPIC, BURN_TOPIC]])
    sw = load_swaps(POOL, cache_root=swap_root)
    assert sw["block_number"].to_list() == [112] and sw["transaction_index"].to_list() == [9]
    liq = load_liquidity_events(POOL, cache_root=tmp_path / "liq")
    assert liq["block_number"].to_list() == [105] and liq["amount"].to_list() == ["7"]
    assert covered_ranges(swap_root / POOL.lower()) == [(100, 109), (110, 119)]


def test_old_swap_files_without_transaction_index_load_with_nulls(tmp_path):
    import polars as pl
    pool_dir = tmp_path / POOL.lower()
    pool_dir.mkdir()
    row = decode_swap_log(make_log(block=150))
    row.pop("transaction_index")
    pl.DataFrame([row]).write_parquet(pool_dir / f"{100:012d}_{199:012d}.parquet")
    df = load_swaps(POOL, cache_root=tmp_path)
    assert df["transaction_index"].to_list() == [None] and df["block_number"].to_list() == [150]
