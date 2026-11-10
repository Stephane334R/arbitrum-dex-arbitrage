"""Parity of data/pricing/v3_swap.py with Uniswap QuoterV2 on Arbitrum One.

tests/vectors/v3_swaps.json is written by `uv run python -m research.parity.quoter_parity`. It
holds, for one pinned block, each pool's state (slot0, liquidity, tickBitmap words, liquidityNet)
and the QuoterV2 answers for 3 pools x 5 sizes x 2 directions.

- test_vectors_* (offline): rebuild each pool from the JSON, rerun the Python port, and require
  *exact* equality with QuoterV2's amountOut, sqrtPriceX96After and initializedTicksCrossed.
- test_*_onchain (integration): the JSON really reflects the chain at that block (tick
  snapshot and quoter answers from the Parquet cache, or from the RPC if not cached).
"""

import json
import os
from pathlib import Path

import pytest

from data.pricing.v3_swap import get_sqrt_ratio_at_tick, get_tick_at_sqrt_ratio
from research.parity.quoter_parity import VECTORS_PATH, python_quote, snapshot_from_json, snapshot_to_json

if not VECTORS_PATH.exists():
    pytest.skip("no vectors yet; run `uv run python -m research.parity.quoter_parity`", allow_module_level=True)

DATA = json.loads(VECTORS_PATH.read_text())
POOLS = {p["address"]: p for p in DATA["pools"]}
VECTORS = DATA["vectors"]


def test_vectors_cover_spec():
    assert len(POOLS) == 3
    for addr in POOLS:
        for z41 in (True, False):
            vs = [v for v in VECTORS if v["pool"] == addr and v["zero_for_one"] == z41]
            assert len(vs) == 5
            assert any(not v["python"]["ticks_crossed"] for v in vs), "need a small, non-crossing size"
            assert any(v["python"]["ticks_crossed"] for v in vs), "need a tick-crossing size"


@pytest.mark.parametrize("v", VECTORS, ids=[v["id"] for v in VECTORS])
def test_vectors_match_quoter_exactly(v):
    snap = snapshot_from_json(POOLS[v["pool"]])
    py = python_quote(snap, v["zero_for_one"], int(v["amount_in"]))
    q = v["quoter"]
    assert py["amount_out"] == q["amount_out"]
    assert py["sqrt_price_x96_after"] == q["sqrt_price_x96_after"]
    assert py["quoter_initialized_ticks_crossed"] == q["initialized_ticks_crossed"]
    assert py == v["python"]  # the stored Python output is what the code produces today


# --- integration ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rpc():
    from dotenv import load_dotenv

    from data.rpc import RpcClient, load_rpc_url

    load_dotenv()
    if not os.environ.get("ARBITRUM_RPC_URL"):
        pytest.skip("ARBITRUM_RPC_URL not set")
    return RpcClient(load_rpc_url())


@pytest.mark.integration
def test_quoter_and_pools_verified_onchain(rpc):
    from data.pools.registry import load_registry
    from research.parity.quoter_parity import verify_pools_at_block, verify_quoter

    registry, block = load_registry(), DATA["meta"]["block"]
    assert verify_quoter(rpc, DATA["meta"]["quoter_v2"]["address"], registry, block)["factory"] == registry["factory"]["address"]
    verify_pools_at_block(rpc, registry, block)


@pytest.mark.integration
def test_pool_snapshots_match_chain(rpc):
    from data.collectors.ticks import get_tick_snapshot

    for addr, p in POOLS.items():
        words = (p["word_hi"] - p["word_lo"]) // 2
        snap = get_tick_snapshot(rpc, addr, DATA["meta"]["block"], words)
        assert snapshot_to_json(snap) == {k: p[k] for k in snapshot_to_json(snap)}


@pytest.mark.integration
def test_quoter_answers_match_chain(rpc):
    from research.parity.quoter_parity import QuoterCache, quote_onchain

    block, quoter = DATA["meta"]["block"], DATA["meta"]["quoter_v2"]["address"]
    cache = QuoterCache(quoter, block)
    for v in VECTORS:
        q = quote_onchain(rpc, cache, quoter, v["token_in"], v["token_out"], v["fee"], int(v["amount_in"]), block)
        assert {k: q[k] for k in v["quoter"]} == v["quoter"], v["id"]
    cache.save()


@pytest.mark.integration
def test_tick_math_against_swap_logs():
    """Every cached Swap log carries the pool's (sqrtPriceX96, tick) after the swap. On-chain
    tick == getTickAtSqrtRatio(price), except when a downward swap ends exactly on a tick
    boundary: the pool then stores tick - 1 (see UniswapV3Pool.swap)."""
    import polars as pl

    from data.collectors.swaps import CACHE_ROOT

    files = list(Path(CACHE_ROOT).glob("*/*.parquet"))
    if not files:
        pytest.skip("no cached swaps; run `uv run python -m data.collectors.swaps` first")
    df = pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")  # newer files add transaction_index
    for s, t in zip(df["sqrt_price_x96"].to_list(), df["tick"].to_list()):
        computed = get_tick_at_sqrt_ratio(int(s))
        on_boundary = get_sqrt_ratio_at_tick(computed) == int(s)
        assert t == computed or (on_boundary and t == computed - 1), (s, t, computed)
