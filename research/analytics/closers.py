"""Who closed the gaps: the transaction that ended each transaction-level episode.

For every closed transaction-level episode (research/replay/txlevel.py), the closing transaction
is the first transaction touching the pair's pools after which the gap was back under the fee
threshold. Its sender, `to`, transaction index and gas used come from
data/collectors/tx_details.py.

DEFINITION, NOT INTENT (labelled): "closer" means the transaction whose state change ended the
gap. That can be a deliberate arbitrage or an ordinary swap that happened to push the price
back. `closer_touches_both_pools` (it had events in both pools of the pair) is the arbitrage
signature reported next to every ranking.

Closer kinds (from the closing transaction's receipt, data/collectors/tx_details.get_tx_swaps):
- both_pools: swapped in both pools of the pair (two-pool arbitrage of exactly this gap);
- multi_pool: swapped in one of the pair's pools and at least one other pool (any venue). That
  is a cross-venue arbitrage OR a multi-hop aggregator route; the receipt alone does not tell
  them apart (labelled, not resolved);
- single_pool: the only recognised swap was in one of the pair's pools. That is a directional
  trade OR a CEX-DEX arbitrage (the other leg is off-chain); also not distinguishable on-chain;
- unrecognised: no recognised Swap event (should not happen: a closer moves a price).
The recognised Swap events are listed in tx_details.SWAP_EVENT_SIGNATURES; the list is not
exhaustive, so multi_pool is a lower bound.

Delays between opening and closing:
- duration_txs: transactions touching the pair's pools, opener excluded, closer included
  (1 = the very next one closed it);
- duration_blocks: close block - open block (0 = same block);
- tx_index_distance: close - open transaction index, same-block episodes only (counts every
  transaction in the block between them, tracked or not).
"""

from __future__ import annotations

import polars as pl


KINDS = ("both_pools", "multi_pool", "single_pool", "unrecognised")


def closer_rows(tx_eps: pl.DataFrame, details: pl.DataFrame, swaps: pl.DataFrame | None = None) -> pl.DataFrame:
    """One row per closed episode with its closing transaction's details (and, with `swaps`,
    its swap pools and closer kind). Raises if a detail row disagrees with the replay on the
    closing transaction's block or index."""
    closed = tx_eps.filter(~pl.col("right_censored"))
    if closed.is_empty():
        return closed
    d = details.rename({"block_number": "_d_block", "transaction_index": "_d_index", "tx_hash": "close_tx_hash"})
    out = closed.join(d, on="close_tx_hash", how="left")
    missing = out.filter(pl.col("sender").is_null())
    if missing.height:
        raise ValueError(f"{missing.height} closing transactions have no details (first: {missing['close_tx_hash'][0]})")
    bad = out.filter((pl.col("_d_block") != pl.col("close_block")) | (pl.col("_d_index") != pl.col("close_tx_index")))
    if bad.height:
        r = bad.row(0, named=True)
        raise ValueError(f"closing tx {r['close_tx_hash']}: chain says block {r['_d_block']} index {r['_d_index']}, "
                         f"replay says {r['close_block']} / {r['close_tx_index']}")
    out = out.drop("_d_block", "_d_index").with_columns(
        (pl.col("gas_used") - pl.col("gas_used_for_l1").fill_null(0)).alias("gas_used_l2"))
    if swaps is None:
        return out
    out = out.join(swaps.rename({"tx_hash": "close_tx_hash"}), on="close_tx_hash", how="left")
    if out["swap_pools"].is_null().any():
        raise ValueError("some closing transactions have no receipt swap data")
    n = pl.col("swap_pools").list.len()
    return out.with_columns(n.alias("n_swap_pools"), pl.when(pl.col("closer_touches_both_pools")).then(pl.lit("both_pools"))
                            .when(n >= 2).then(pl.lit("multi_pool")).when(n == 1).then(pl.lit("single_pool"))
                            .otherwise(pl.lit("unrecognised")).alias("closer_kind"))


def _median(s: pl.Series):
    s = s.drop_nulls()
    return float(s.median()) if s.len() else None


def _ranking(c: pl.DataFrame, key: str, n: int) -> pl.DataFrame:
    total = c.height
    return (c.group_by(key).agg(
        pl.len().alias("gaps_closed"),
        pl.col("to").mode().first().alias("top_to") if key == "sender" else pl.col("sender").n_unique().alias("senders"),
        pl.col("closer_touches_both_pools").mean().alias("both_pools_share"),
        *([(pl.col("closer_kind") == "multi_pool").mean().alias("multi_pool_share"),
           (pl.col("closer_kind") == "single_pool").mean().alias("single_pool_share")] if "closer_kind" in c.columns else []),
        pl.col("duration_txs").median().alias("median_delay_txs"),
        pl.col("duration_blocks").median().alias("median_delay_blocks"),
        pl.col("gas_used").median().alias("median_gas_used"),
        pl.col("peak_gross_usdc").sum().alias("gross_usdc_of_gaps"),
    ).with_columns((pl.col("gaps_closed") / total).alias("share"))
        .sort(["gaps_closed", key], descending=[True, False]).head(n))


def summarize(c: pl.DataFrame, top_n: int = 10) -> dict:
    """Rankings by sender and by `to` contract, top-5 shares and overall delay medians."""
    if c.is_empty():
        return {"n_closed": 0}
    by_sender, by_to = _ranking(c, "sender", top_n), _ranking(c.filter(pl.col("to").is_not_null()), "to", top_n)
    arb = c.filter(pl.col("closer_touches_both_pools"))
    other = c.filter(~pl.col("closer_touches_both_pools"))
    return {
        "n_closed": c.height,
        "n_closing_txs": c["close_tx_hash"].n_unique(),
        "next_tx_share": float((c["duration_txs"] == 1).mean()),
        "same_block_share": float((c["duration_blocks"] == 0).mean()),
        "n_senders": c["sender"].n_unique(),
        "n_to": c["to"].n_unique(),
        "top5_share_sender": float(by_sender.head(5)["gaps_closed"].sum() / c.height),
        "top5_share_to": float(by_to.head(5)["gaps_closed"].sum() / c.height),
        "top1_share_sender": float(by_sender["gaps_closed"][0] / c.height),
        "both_pools_share": float(c["closer_touches_both_pools"].mean()),
        "failed_closers": int((c["status"] == 0).sum()),
        "median_delay_txs": _median(c["duration_txs"]),
        "median_delay_blocks": _median(c["duration_blocks"]),
        "median_tx_index_distance": _median(c["tx_index_distance"]),
        "median_close_tx_index": _median(c["close_tx_index"]),
        "median_gas_used": _median(c["gas_used"]),
        "median_delay_txs_both_pools": _median(arb["duration_txs"]),
        "median_delay_blocks_both_pools": _median(arb["duration_blocks"]),
        "median_delay_txs_other": _median(other["duration_txs"]),
        "median_delay_blocks_other": _median(other["duration_blocks"]),
        "by_sender": by_sender,
        "by_to": by_to,
        **(_kinds(c) if "closer_kind" in c.columns else {}),
    }


def _kinds(c: pl.DataFrame) -> dict:
    """Share, delays and gross by closer kind."""
    rows = []
    for k in KINDS:
        g = c.filter(pl.col("closer_kind") == k)
        rows.append({"kind": k, "n": g.height, "share": g.height / c.height,
                     "median_delay_txs": _median(g["duration_txs"]), "median_delay_blocks": _median(g["duration_blocks"]),
                     "same_block_share": float((g["duration_blocks"] == 0).mean()) if g.height else None,
                     "median_close_tx_index": _median(g["close_tx_index"]),
                     "median_gas_used": _median(g["gas_used"]), "gross_usdc": float(g["peak_gross_usdc"].sum())})
    return {"by_kind": rows}


def short(addr: str | None) -> str:
    return "contract creation" if addr is None else f"{addr[:6]}…{addr[-4:]}"
