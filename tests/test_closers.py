"""Tests for research/analytics/closers.py and data/collectors/tx_details.py (no RPC)."""

import polars as pl
import pytest

from data.collectors.tx_details import (SWAP_EVENT_SIGNATURES, fetch_tx_details, get_tx_details, get_tx_swaps,
                                       receipt_swap_pools)
from eth_utils import keccak
from research.analytics import charts
from research.analytics.closers import closer_rows, short, summarize

A, B, C = "0x" + "aa" * 20, "0x" + "bb" * 20, "0x" + "cc" * 20
BOT, ROUTER = "0x" + "11" * 20, "0x" + "22" * 20


def _eps():
    # 5 closed episodes + 1 still open at the window end (no closer).
    rows = [
        # close_tx_hash, close_block, close_tx_index, duration_txs, duration_blocks, tx_index_distance, both, gross
        ("0x01", 100, 3, 1, 0, 2, True, 0.03),
        ("0x02", 105, 1, 1, 1, None, True, 0.01),
        ("0x03", 110, 9, 2, 0, 7, False, 0.002),
        ("0x04", 120, 4, 1, 0, 1, True, 0.02),
        ("0x05", 130, 2, 5, 3, None, False, 0.001),
    ]
    eps = [{"pair": "100-500", "close_tx_hash": h, "close_block": b, "close_tx_index": i, "duration_txs": dt,
            "duration_blocks": db, "tx_index_distance": dist, "closer_touches_both_pools": both,
            "peak_gross_usdc": g, "right_censored": False} for h, b, i, dt, db, dist, both, g in rows]
    eps.append({"pair": "100-500", "close_tx_hash": None, "close_block": None, "close_tx_index": None,
                "duration_txs": 0, "duration_blocks": 4, "tx_index_distance": None, "closer_touches_both_pools": False,
                "peak_gross_usdc": 0.5, "right_censored": True})
    return pl.DataFrame(eps)


def _details(override=None):
    rows = [("0x01", 100, 3, A, BOT), ("0x02", 105, 1, A, BOT), ("0x03", 110, 9, B, ROUTER),
            ("0x04", 120, 4, C, BOT), ("0x05", 130, 2, B, ROUTER)]
    d = [{"tx_hash": h, "block_number": b, "transaction_index": i, "sender": s, "to": t, "gas_used": 300_000 + k,
          "gas_used_for_l1": 0, "effective_gas_price_wei": 10**7, "status": 1, "input_selector": "0x12345678"}
         for k, (h, b, i, s, t) in enumerate(rows)]
    if override:
        d[0].update(override)
    return pl.DataFrame(d)


def test_closer_rows_join_and_skip_open_episodes():
    c = closer_rows(_eps(), _details())
    assert c.height == 5  # the right-censored episode has no closer
    assert c.filter(pl.col("close_tx_hash") == "0x03")["sender"][0] == B
    assert c["gas_used_l2"][0] == 300_000


def test_closer_rows_rejects_index_disagreeing_with_the_replay():
    with pytest.raises(ValueError, match="replay says"):
        closer_rows(_eps(), _details({"transaction_index": 4}))


def test_closer_rows_rejects_missing_details():
    with pytest.raises(ValueError, match="no details"):
        closer_rows(_eps(), _details().filter(pl.col("tx_hash") != "0x05"))


def test_summarize_rankings_shares_and_delays():
    s = summarize(closer_rows(_eps(), _details()))
    assert s["n_closed"] == 5 and s["n_senders"] == 3 and s["n_to"] == 2
    top = s["by_sender"].row(0, named=True)
    # A and B both closed 2; ties broken by address: A (0xaa..) first.
    assert (top["sender"], top["gaps_closed"], top["share"], top["top_to"]) == (A, 2, 0.4, BOT)
    assert top["both_pools_share"] == 1.0 and top["median_delay_blocks"] == 0.5
    assert s["top1_share_sender"] == 0.4 and s["top5_share_sender"] == 1.0
    assert s["by_to"].row(0, named=True)["to"] == BOT and s["by_to"].row(0, named=True)["senders"] == 2
    assert s["both_pools_share"] == pytest.approx(0.6)
    assert s["median_delay_txs"] == 1 and s["median_delay_blocks"] == 0
    assert s["median_tx_index_distance"] == 2  # same-block episodes only: 2, 7, 1
    assert (s["median_delay_txs_both_pools"], s["median_delay_txs_other"]) == (1, 3.5)


def test_summarize_empty():
    assert summarize(pl.DataFrame()) == {"n_closed": 0}


def _topic(kind):
    return "0x" + keccak(text=SWAP_EVENT_SIGNATURES[kind]).hex()


P1, P2, PM = "0x" + "01" * 20, "0x" + "02" * 20, "0x" + "04" * 20


def _swaps():
    # 0x01/0x02/0x04: both pools (2 v3 swaps); 0x03, 0x05: only one pool; 0x04 also hits a v4 pool.
    return pl.DataFrame([
        {"tx_hash": "0x01", "n_logs": 6, "swap_pools": [P1, P2], "swap_kinds": ["v3", "v3"]},
        {"tx_hash": "0x02", "n_logs": 6, "swap_pools": [P1, P2], "swap_kinds": ["v3", "v3"]},
        {"tx_hash": "0x03", "n_logs": 3, "swap_pools": [P1], "swap_kinds": ["v3"]},
        {"tx_hash": "0x04", "n_logs": 9, "swap_pools": [P1, PM + ":0xab"], "swap_kinds": ["v3", "v4"]},
        {"tx_hash": "0x05", "n_logs": 3, "swap_pools": [P2], "swap_kinds": ["v3"]},
    ])


def test_receipt_swap_pools_dedupes_and_keys_singletons():
    rc = {"logs": [
        {"address": P1.upper().replace("0X", "0x"), "topics": [_topic("v3"), "0x0"]},
        {"address": P1, "topics": [_topic("v3")]},  # same pool twice -> once
        {"address": PM, "topics": [_topic("v4"), "0xAB"]},
        {"address": PM, "topics": [_topic("v4"), "0xcd"]},  # same singleton, other pool id
        {"address": P2, "topics": ["0x" + "ee" * 32]},  # not a swap
        {"address": P2, "topics": []},
    ]}
    assert receipt_swap_pools(rc) == ([P1, PM + ":0xab", PM + ":0xcd"], ["v3", "v4", "v4"])


def test_closer_kinds_and_summary():
    c = closer_rows(_eps(), _details(), _swaps())
    kinds = dict(zip(c["close_tx_hash"], c["closer_kind"]))
    # 0x04 touches both pools per the replay flag, so it is both_pools even with a v4 leg.
    assert kinds == {"0x01": "both_pools", "0x02": "both_pools", "0x03": "single_pool", "0x04": "both_pools",
                     "0x05": "single_pool"}
    s = summarize(c)
    by_kind = {r["kind"]: r for r in s["by_kind"]}
    assert by_kind["both_pools"]["n"] == 3 and by_kind["single_pool"]["n"] == 2 and by_kind["multi_pool"]["n"] == 0
    assert s["n_closing_txs"] == 5 and s["next_tx_share"] == 0.6 and s["same_block_share"] == 0.6
    assert s["by_to"].row(1, named=True)["single_pool_share"] == 1.0  # ROUTER closed 0x03 and 0x05


def test_closer_kind_multi_pool_when_not_flagged_both():
    eps = _eps().with_columns(pl.lit(False).alias("closer_touches_both_pools"))
    c = closer_rows(eps, _details(), _swaps())
    assert dict(zip(c["close_tx_hash"], c["closer_kind"]))["0x04"] == "multi_pool"


def test_closer_rows_rejects_missing_swap_data():
    with pytest.raises(ValueError, match="receipt swap data"):
        closer_rows(_eps(), _details(), _swaps().filter(pl.col("tx_hash") != "0x01"))


def test_get_tx_swaps_caches(tmp_path):
    class Fake:
        calls = 0

        def call(self, method, params):
            Fake.calls += 1
            return {"logs": [{"address": P1, "topics": [_topic("v2")]}]}

    d = get_tx_swaps(Fake(), ["0xAA"], path=tmp_path / "s.parquet")
    assert d.row(0, named=True) == {"tx_hash": "0xaa", "n_logs": 1, "swap_pools": [P1], "swap_kinds": ["v2"]}
    get_tx_swaps(Fake(), ["0xaa"], path=tmp_path / "s.parquet")
    assert Fake.calls == 1


def test_closers_chart_renders(tmp_path):
    s = summarize(closer_rows(_eps(), _details(), _swaps()))
    charts.closers_bar(s["by_to"], s["n_closed"], tmp_path / "c.png", key="to")
    charts.closers_bar(s["by_sender"], s["n_closed"], tmp_path / "s.png", key="sender")
    assert (tmp_path / "c.png").stat().st_size > 1000 and (tmp_path / "s.png").stat().st_size > 1000


def test_fetch_tx_details_parses_arbitrum_receipt(tmp_path):
    class Fake:
        calls = 0

        def call(self, method, params):
            Fake.calls += 1
            if method == "eth_getTransactionByHash":
                return {"blockNumber": hex(100), "transactionIndex": hex(3), "from": A, "to": BOT,
                        "input": "0xdeadbeef" + "00" * 32}
            return {"gasUsed": hex(310_000), "gasUsedForL1": hex(0), "effectiveGasPrice": hex(10**7), "status": "0x1"}

    r = fetch_tx_details(Fake(), "0xABC")
    assert (r["tx_hash"], r["transaction_index"], r["gas_used"], r["input_selector"]) == ("0xabc", 3, 310_000, "0xdeadbeef")
    get_tx_details(Fake(), ["0xabc"], path=tmp_path / "d.parquet")
    n = Fake.calls
    assert get_tx_details(Fake(), ["0xABC"], path=tmp_path / "d.parquet").height == 1 and Fake.calls == n  # cached


def test_short_address():
    assert short(A) == "0xaaaa…aaaa" and short(None) == "contract creation"


def test_render_closer_section():
    from research.replay.run import render_closer_section

    text = "\n".join(render_closer_section(summarize(closer_rows(_eps(), _details(), _swaps()))))
    assert "What this says about competition" in text and "closers_top_contracts.png" in text
    assert "Two-pool arbitrage of these pairs" in text  # 60% both-pools here, so not "almost nobody"
    assert "| Only one pool of the pair" in text
    assert "| One pool of the pair + other pools" not in text  # kinds with 0 closers get no row
