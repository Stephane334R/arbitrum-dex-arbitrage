"""Tests for research/replay/txlevel.py and the Mint/Burn collector decoding.

Fixtures are synthetic (fake pool addresses, hand-made prices); no RPC."""

import math

import polars as pl
import pytest
from eth_abi import encode

from data.collectors.liquidity_events import BURN_TOPIC, MINT_TOPIC, decode_liquidity_log
from research.replay.replay import PoolMeta, build_segments, find_episodes, pair_rows
from research.replay.txlevel import (
    apply_event,
    check_order,
    checkpoint_blocks,
    compare_levels,
    find_tx_episodes,
    ordered_events,
    quality_checks,
    replay_transactions,
    state_at_end_of_block,
    tx_pair_rows,
)

WETH, USDC = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
POOLS = ["0x" + "a1" * 20, "0x" + "b2" * 20]  # fake addresses
FEES = [100, 500]
REGISTRY = {
    "tokens": {"WETH": {"address": WETH, "decimals": 18}, "USDC": {"address": USDC, "decimals": 6}},
    "pools": [{"address": a, "fee": f, "tick_spacing": 1 if f == 100 else 10,
               "token0": {"address": WETH, "decimals": 18}, "token1": {"address": USDC, "decimals": 6}}
              for a, f in zip(POOLS, FEES)],
}
METAS = [PoolMeta(a, f, 1 if f == 100 else 10, True, 18, 6) for a, f in zip(POOLS, FEES)]
WINDOW = (100, 109)
L = 10**18


def sqrt_tick(price):
    raw = price * 1e6 / 1e18
    return int(math.sqrt(raw) * 2**96), math.floor(math.log(raw) / math.log(1.0001))


def swap(block, txi, log, price, liq=L, ts=None, h=None):
    s, t = sqrt_tick(price)
    return {"block_number": block, "block_timestamp": ts if ts is not None else block, "tx_hash": h or f"0x{block:04x}{txi:04x}",
            "log_index": log, "sqrt_price_x96": str(s), "liquidity": str(liq), "tick": t, "_txi": txi}


def swaps_df(rows):
    cols = ["block_number", "block_timestamp", "tx_hash", "log_index", "sqrt_price_x96", "liquidity", "tick"]
    return pl.DataFrame([{c: r[c] for c in cols} for r in rows],
                        schema={"block_number": pl.Int64, "block_timestamp": pl.Int64, "tx_hash": pl.Utf8,
                                "log_index": pl.Int32, "sqrt_price_x96": pl.Utf8, "liquidity": pl.Utf8, "tick": pl.Int32})


def liq_df(rows):
    schema = {"block_number": pl.Int64, "block_timestamp": pl.Int64, "tx_hash": pl.Utf8, "transaction_index": pl.Int32,
              "log_index": pl.Int32, "event": pl.Utf8, "tick_lower": pl.Int32, "tick_upper": pl.Int32, "amount": pl.Utf8}
    return pl.DataFrame(rows, schema=schema)


def initial(price, liq=L):
    s, t = sqrt_tick(price)
    return {"sqrt_price_x96": s, "tick": t, "liquidity": liq}


def build(swaps_a, swaps_b, liq_a=(), liq_b=()):
    tx_index = {r["tx_hash"]: r["_txi"] for r in [*swaps_a, *swaps_b]}
    return ordered_events([swaps_df(swaps_a), swaps_df(swaps_b)], [liq_df(list(liq_a)), liq_df(list(liq_b))],
                          tx_index, WINDOW)


# ---------------------------------------------------------------- Mint/Burn decoding


def test_mint_burn_topics():
    assert MINT_TOPIC == "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde"
    assert BURN_TOPIC == "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"


def _int24_topic(v):
    return "0x" + encode(["int24"], [v]).hex()


@pytest.mark.parametrize("kind", ["mint", "burn"])
def test_decode_liquidity_log(kind):
    owner = "0x" + "00" * 12 + "33" * 20
    if kind == "mint":
        data = encode(["address", "uint128", "uint256", "uint256"], ["0x" + "44" * 20, 2**100, 5, 6])
        topic = MINT_TOPIC
    else:
        data = encode(["uint128", "uint256", "uint256"], [2**100, 5, 6])
        topic = BURN_TOPIC
    log = {"address": POOLS[0], "topics": [topic, owner, _int24_topic(-200_000), _int24_topic(-199_990)],
           "data": "0x" + data.hex(), "blockNumber": hex(7), "blockTimestamp": hex(9), "transactionHash": "0xab",
           "transactionIndex": hex(4), "logIndex": hex(12)}
    r = decode_liquidity_log(log)
    assert r["event"] == kind and r["amount"] == str(2**100)
    assert (r["tick_lower"], r["tick_upper"]) == (-200_000, -199_990)  # negative int24 from topics
    assert (r["block_number"], r["transaction_index"], r["log_index"]) == (7, 4, 12)
    assert r["owner"].lower() == "0x" + "33" * 20


# ---------------------------------------------------------------- 1. event ordering


def test_events_sorted_by_block_tx_index_log_index():
    a = [swap(101, 5, 9, 2000.0), swap(101, 1, 2, 2001.0)]
    b = [swap(101, 3, 4, 2002.0), swap(100, 7, 1, 2003.0)]
    mint = [{"block_number": 101, "block_timestamp": 101, "tx_hash": f"0x{101:04x}{3:04x}", "transaction_index": 3, "log_index": 5,
             "event": "mint", "tick_lower": -10**6, "tick_upper": 10**6, "amount": "1"}]
    ev = build(a, b, liq_a=mint)
    assert list(zip(ev["block_number"], ev["transaction_index"], ev["log_index"])) == [
        (100, 7, 1), (101, 1, 2), (101, 3, 4), (101, 3, 5), (101, 5, 9)]
    assert ev["kind"].to_list() == ["swap", "swap", "swap", "mint", "swap"]


def test_check_order_rejects_log_index_against_tx_order():
    # tx 1 has log index 8, tx 2 has log index 3: impossible if logIndex follows execution.
    with pytest.raises(ValueError, match="log order"):
        build([swap(101, 1, 8, 2000.0)], [swap(101, 2, 3, 2000.0)])


def test_check_order_rejects_tx_hash_with_two_indices():
    ev = pl.DataFrame({"block_number": [101, 101], "transaction_index": [1, 2], "log_index": [1, 2],
                       "tx_hash": ["0xa", "0xa"]})
    with pytest.raises(ValueError, match="more than one"):
        check_order(ev)


def test_events_in_one_tx_give_one_state_row():
    h = "0xsame"
    ev = build([swap(101, 4, 1, 2010.0, h=h)], [swap(101, 4, 2, 2011.0, h=h)])
    st = replay_transactions(ev, [initial(2000.0), initial(2000.0)], WINDOW, 100)
    assert st.height == 2  # seed + one transaction
    r = st.row(1, named=True)
    assert r["touched_mask"] == 0b11 and r["n_events"] == 2 and r["transaction_index"] == 4


# ---------------------------------------------------------------- 2. Mint/Burn liquidity updates


def _liq_ev(kind, lo, hi, amount):
    return {"kind": kind, "tick_lower": lo, "tick_upper": hi, "amount": str(amount), "block_number": 1}


def test_mint_in_range_adds_liquidity():
    s = {"sqrt": 1, "tick": 50, "liq": 1000}
    assert apply_event(s, _liq_ev("mint", 0, 100, 7))["liq"] == 1007
    assert apply_event(s, _liq_ev("burn", 0, 100, 7))["liq"] == 993


def test_out_of_range_position_does_not_change_liquidity():
    s = {"sqrt": 1, "tick": 50, "liq": 1000}
    assert apply_event(s, _liq_ev("mint", 60, 100, 7)) == s
    assert apply_event(s, _liq_ev("mint", 0, 40, 7)) == s


def test_range_bounds_lower_inclusive_upper_exclusive():
    # UniswapV3Pool._modifyPosition: in range iff tickLower <= tick < tickUpper.
    assert apply_event({"sqrt": 1, "tick": 0, "liq": 0}, _liq_ev("mint", 0, 10, 5))["liq"] == 5
    assert apply_event({"sqrt": 1, "tick": 10, "liq": 0}, _liq_ev("mint", 0, 10, 5))["liq"] == 0


def test_burn_below_zero_raises():
    with pytest.raises(ValueError, match="negative liquidity"):
        apply_event({"sqrt": 1, "tick": 5, "liq": 3}, _liq_ev("burn", 0, 10, 4))


def test_mint_between_swaps_changes_replayed_liquidity_and_next_swap_resets_it():
    s0, t0 = sqrt_tick(2000.0)
    mint = {"block_number": 102, "block_timestamp": 102, "tx_hash": "0xm", "transaction_index": 0, "log_index": 0,
            "event": "mint", "tick_lower": t0 - 100, "tick_upper": t0 + 100, "amount": str(5 * L)}
    out_of_range = {**mint, "tx_hash": "0xo", "transaction_index": 1, "log_index": 1, "tick_lower": t0 + 10,
                    "tick_upper": t0 + 20}
    ev = build([swap(103, 0, 0, 2000.0, liq=4 * L)], [], liq_a=[mint, out_of_range])
    st = replay_transactions(ev, [initial(2000.0), initial(2000.0)], WINDOW, 100)
    assert [int(x) for x in st["liq_0"]] == [L, 6 * L, 6 * L, 4 * L]
    eob = state_at_end_of_block(st, [100, 102, 103])
    assert int(eob[100]["liq_0"]) == L and int(eob[102]["liq_0"]) == 6 * L and int(eob[103]["liq_0"]) == 4 * L


# ---------------------------------------------------------------- 3-5. episodes: intra-block fixture


def _block_level(swaps_a, swaps_b, init):
    seg = build_segments({POOLS[0]: swaps_df(swaps_a), POOLS[1]: swaps_df(swaps_b)},
                         {POOLS[0]: init[0], POOLS[1]: init[1]}, WINDOW, 100)
    return find_episodes(pair_rows(seg, METAS, REGISTRY), WINDOW, window_end_close_ts=109)


def _tx_level(swaps_a, swaps_b, init):
    ev = build(swaps_a, swaps_b)
    st = replay_transactions(ev, init, WINDOW, 100)
    return find_tx_episodes(tx_pair_rows(st, METAS, REGISTRY), WINDOW, window_end_close_ts=109)


def test_intra_block_episode_is_found_and_missed_by_block_level():
    """Block 103: tx 2 pushes pool A to 2010 (50 bps gap > 6 bps fees), tx 6 swaps both pools
    back into line. The gap opens and closes inside block 103."""
    init = [initial(2000.0), initial(2000.0)]
    arb = "0xarb"
    a = [swap(103, 2, 10, 2010.0), swap(103, 6, 20, 2000.4, h=arb)]
    b = [swap(103, 6, 21, 2000.2, h=arb)]
    blk = _block_level(a, b, init)
    tx = _tx_level(a, b, init)
    assert blk.is_empty()
    assert tx.height == 1
    e = tx.row(0, named=True)
    assert e["intra_block"] and not e["visible_at_block_level"]
    assert (e["start_block"], e["start_tx_index"], e["close_block"], e["close_tx_index"]) == (103, 2, 103, 6)
    assert e["duration_txs"] == 1 and e["duration_blocks"] == 0 and e["tx_index_distance"] == 4
    assert e["closer_touches_both_pools"]
    assert e["peak_gap_bps"] == pytest.approx(50, abs=0.1)
    assert e["peak_gross_usdc"] > 0
    tx, cmp = compare_levels(tx, blk, WINDOW)
    assert cmp["consistent"] and cmp["n_missed"] == 1 and cmp["n_visible"] == 0
    assert tx["block_episode_idx"].to_list() == [None]


def test_two_intra_block_episodes_and_one_visible_episode():
    """Block 103 has two separate intra-block gaps; a third gap opens late in block 105 and
    closes in block 107, so the block level sees it (open at the end of blocks 105-106)."""
    init = [initial(2000.0), initial(2000.0)]
    a = [swap(103, 1, 1, 2010.0), swap(103, 2, 2, 2000.0), swap(103, 5, 5, 1990.0), swap(103, 8, 8, 2000.0),
         swap(105, 9, 3, 2012.0), swap(107, 0, 1, 2000.0)]
    b = [swap(104, 0, 0, 2000.1)]  # touches the pair, gap below threshold
    blk = _block_level(a, b, init)
    tx = _tx_level(a, b, init)
    assert blk.height == 1 and (blk["start_block"][0], blk["end_block"][0]) == (105, 106)
    tx = tx.sort("start_seq")
    assert tx["intra_block"].to_list() == [True, True, False]
    assert tx["duration_blocks"].to_list() == [0, 0, 2]
    v = tx.row(2, named=True)
    assert v["visible_at_block_level"] and v["duration_txs"] == 1 and v["tx_index_distance"] is None
    tx, cmp = compare_levels(tx, blk, WINDOW)
    assert cmp == {"consistent": True, "n_tx": 3, "n_block": 1, "n_missed": 2, "n_visible": 1,
                   "n_block_matched": 1, "n_block_with_several_tx_episodes": 0}


def test_block_level_episode_split_into_two_tx_episodes():
    """Gap open at the end of block 103, briefly closed inside block 104 (tx 2 -> tx 4) and open
    again at its end: one block-level episode, two transaction-level episodes."""
    init = [initial(2000.0), initial(2000.0)]
    a = [swap(103, 0, 0, 2010.0), swap(104, 2, 2, 2000.0), swap(104, 4, 4, 2010.0), swap(105, 1, 1, 2000.0)]
    blk = _block_level(a, [], init)
    tx = _tx_level(a, [], init)
    assert blk.height == 1 and (blk["start_block"][0], blk["end_block"][0]) == (103, 104)
    tx, cmp = compare_levels(tx, blk, WINDOW)
    assert cmp["consistent"] and cmp["n_missed"] == 0 and cmp["n_block_with_several_tx_episodes"] == 1
    assert tx["block_episode_idx"].to_list() == [0, 0]


def test_right_censored_tx_episode():
    init = [initial(2000.0), initial(2000.0)]
    tx = _tx_level([swap(108, 3, 1, 2010.0)], [], init)
    e = tx.row(0, named=True)
    assert e["right_censored"] and e["visible_at_block_level"] and e["close_block"] is None
    assert e["duration_blocks"] == 110 - 108 and e["duration_txs"] == 0 and e["duration_s"] == 109 - 108


# ---------------------------------------------------------------- data quality


def test_quality_checks_flags_stale_swap_only_liquidity():
    s0, t0 = sqrt_tick(2000.0)
    mint = {"block_number": 102, "block_timestamp": 102, "tx_hash": "0xm", "transaction_index": 0, "log_index": 0,
            "event": "mint", "tick_lower": t0 - 1, "tick_upper": t0 + 1, "amount": str(L)}
    ev = build([], [], liq_a=[mint])
    init = [initial(2000.0), initial(2000.0)]
    st = replay_transactions(ev, init, WINDOW, 100)
    chain = {(POOLS[0], 105): {"sqrt_price_x96": s0, "tick": t0, "liquidity": 2 * L},
             (POOLS[1], 105): {"sqrt_price_x96": s0, "tick": t0, "liquidity": L}}
    q = quality_checks(st, [swaps_df([]), swaps_df([])], init, POOLS, [105], lambda p, b: chain[(p, b)])
    assert q["match"].to_list() == [True, True]
    assert q["swap_only_liq_match"].to_list() == [False, True]


def test_checkpoint_blocks_include_window_end():
    assert checkpoint_blocks((100, 109), 4) == [103, 107, 109]
    assert checkpoint_blocks((100, 107), 4) == [103, 107]
