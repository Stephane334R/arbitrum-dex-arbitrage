"""Transaction-level replay: pool state after every transaction, gaps and episodes inside blocks.

The block-level replay (replay.py) sees each pool once per block, at the end of the block. This
module rebuilds the state after every transaction that touches a tracked pool, so it also sees
gaps that open and close inside one block.

Method
------
1. Events. Swap, Mint and Burn logs of the tracked pools, sorted by
   (block_number, transaction_index, log_index). `check_order` asserts that this order agrees
   with the block-wide logIndex (which nodes assign in execution order) and that every tx hash
   has a single (block, index).
2. State. Seeded with slot0()/liquidity() at the block before the window. Then, per event:
   - Swap: sqrtPriceX96, tick and in-range liquidity are set to the values in the event. EXACT:
     the event carries the pool's post-swap slot0.sqrtPriceX96, slot0.tick and liquidity.
   - Mint / Burn: liquidity += / -= amount if tickLower <= tick < tickUpper, which is the
     pool's own rule (UniswapV3Pool._modifyPosition). Price and tick do not change.
   No other pool function changes these three values (flash, collect and protocol-fee
   functions do not), so with complete logs the reconstruction is exact. `quality_checks`
   tests that claim against archive slot0()/liquidity() every N blocks.
3. Transactions. The state is recorded after each transaction, not after each event: a state in
   the middle of a transaction cannot be traded against by anyone else.
4. Gaps and episodes. For each pool pair, only the transactions that touch one of the pair's
   two pools can change its gap, so the pair's rows are those transactions (plus the seed). The
   gap rule (sum of fees) and the single-tick sizing are those of replay.py. Consecutive
   above-threshold rows form an episode: it opens with the transaction that made the gap exceed
   the threshold and closes with the first transaction after which it no longer does. Episodes
   can now start and end in the same block.
5. Block-level comparison. A transaction-level episode is visible to the block-level replay iff
   it is still open at the end of some block (close_block > start_block, or right-censored).
   The others are the intra-block episodes the block-level replay missed. `compare_levels`
   also checks that the blocks during which transaction-level episodes are open at the end of
   the block equal the blocks of the block-level episodes (both read the same exact prices).

Simplifications (labelled):
- Peak gross profit uses the single-tick formula on the exact replayed state for the peak
  choice, then the exact multi-tick swap port at the peak transaction (`size_tx_episodes`). The
  multi-tick tick table is read at the END of the block before the peak, with
  sqrtPriceX96/tick/liquidity replaced by the replayed intra-block state. That table is exact
  unless a Mint/Burn of that pool earlier in the same block changed an initialized tick:
  `mt_tick_table_exact` flags it.
- duration_txs counts transactions that touch the pair's two pools, not every transaction on
  the chain. tx_index_distance (same-block episodes only) counts every transaction in the block
  between the opener and the closer.
- As in the block-level replay: perfect execution, no competition, pairs evaluated
  independently, one gas estimate per pair.
"""

from __future__ import annotations

import bisect
from dataclasses import replace
from itertools import combinations

import polars as pl

from research.replay.replay import PoolMeta, pair_rows

EVENT_SCHEMA = {
    "pool_idx": pl.Int32,
    "block_number": pl.Int64,
    "block_timestamp": pl.Int64,
    "tx_hash": pl.Utf8,
    "transaction_index": pl.Int32,
    "log_index": pl.Int32,
    "kind": pl.Utf8,  # swap | mint | burn
    "sqrt_price_x96": pl.Utf8,
    "liquidity": pl.Utf8,
    "tick": pl.Int32,
    "tick_lower": pl.Int32,
    "tick_upper": pl.Int32,
    "amount": pl.Utf8,
}


# ---------------------------------------------------------------- 1. ordered events


def ordered_events(swaps: list[pl.DataFrame], liquidity: list[pl.DataFrame], swap_tx_index: dict[str, int],
                   window: tuple[int, int]) -> pl.DataFrame:
    """All Swap/Mint/Burn events of the pools (list position = pool index) in the window, sorted
    by (block, transaction index, log index). `swap_tx_index` maps lowercase tx hash ->
    transactionIndex for swap transactions (the swap cache has no index column)."""
    lo, hi = window
    rows = []
    for i, df in enumerate(swaps):
        for r in df.filter(pl.col("block_number").is_between(lo, hi)).iter_rows(named=True):
            h = r["tx_hash"].lower()
            rows.append({"pool_idx": i, "block_number": r["block_number"], "block_timestamp": r["block_timestamp"],
                         "tx_hash": h, "transaction_index": swap_tx_index[h], "log_index": r["log_index"],
                         "kind": "swap", "sqrt_price_x96": r["sqrt_price_x96"], "liquidity": r["liquidity"],
                         "tick": r["tick"], "tick_lower": None, "tick_upper": None, "amount": None})
    for i, df in enumerate(liquidity):
        for r in df.filter(pl.col("block_number").is_between(lo, hi)).iter_rows(named=True):
            rows.append({"pool_idx": i, "block_number": r["block_number"], "block_timestamp": r["block_timestamp"],
                         "tx_hash": r["tx_hash"].lower(), "transaction_index": r["transaction_index"],
                         "log_index": r["log_index"], "kind": r["event"], "sqrt_price_x96": None,
                         "liquidity": None, "tick": None, "tick_lower": r["tick_lower"],
                         "tick_upper": r["tick_upper"], "amount": r["amount"]})
    df = pl.DataFrame(rows, schema=EVENT_SCHEMA).sort(["block_number", "transaction_index", "log_index"])
    check_order(df)
    return df


def check_order(events: pl.DataFrame) -> None:
    """Raise if the (block, tx index, log index) order is inconsistent: log indices must be
    unique and increasing within each block, and each tx hash must have one (block, index)."""
    if events.is_empty():
        return
    bad = events.with_columns(
        (pl.col("log_index").diff().over("block_number") <= 0).alias("_bad")).filter(pl.col("_bad"))
    if bad.height:
        r = bad.row(0, named=True)
        raise ValueError(f"log order disagrees with transaction order in block {r['block_number']}")
    per_tx = events.group_by("tx_hash").agg(pl.col("block_number").n_unique().alias("nb"),
                                            pl.col("transaction_index").n_unique().alias("ni"))
    if per_tx.filter((pl.col("nb") > 1) | (pl.col("ni") > 1)).height:
        raise ValueError("a transaction hash maps to more than one (block, transaction index)")
    per_idx = events.group_by("block_number", "transaction_index").agg(pl.col("tx_hash").n_unique().alias("nh"))
    if per_idx.filter(pl.col("nh") > 1).height:
        raise ValueError("two transaction hashes share one (block, transaction index)")


# ---------------------------------------------------------------- 2-3. state after every transaction


def apply_event(state: dict, ev: dict) -> dict:
    """Pool state {sqrt, tick, liq} (ints) after one event; returns a new dict."""
    if ev["kind"] == "swap":
        return {"sqrt": int(ev["sqrt_price_x96"]), "tick": int(ev["tick"]), "liq": int(ev["liquidity"])}
    if ev["kind"] not in ("mint", "burn"):
        raise ValueError(f"unknown event kind {ev['kind']!r}")
    if not (ev["tick_lower"] <= state["tick"] < ev["tick_upper"]):
        return state  # position out of range: in-range liquidity unchanged
    delta = int(ev["amount"]) * (1 if ev["kind"] == "mint" else -1)
    liq = state["liq"] + delta
    if liq < 0:
        raise ValueError(f"negative liquidity after {ev['kind']} at block {ev['block_number']}: events missing")
    return {**state, "liq": liq}


def replay_transactions(events: pl.DataFrame, initial: list[dict], window: tuple[int, int],
                        window_start_ts: int) -> pl.DataFrame:
    """One row per transaction that touches a tracked pool, plus a seed row (seq 0, block
    lo - 1, transaction_index -1: the state at the end of the block before the window; its ts is
    the window's first block timestamp, as in the block-level replay).

    Columns: seq, block_number, transaction_index, tx_hash, ts, touched_mask (bit i = pool i
    had an event in this tx), n_events, and per pool i: sqrt_i, liq_i (base-10 strings), tick_i.
    Also block_start = block_end = block_number and ts_start = ts, so replay.pair_rows can read it.
    """
    n = len(initial)
    state = [{"sqrt": int(s["sqrt_price_x96"]), "tick": int(s["tick"]), "liq": int(s["liquidity"])} for s in initial]

    def row(seq, block, txi, h, ts, mask, n_ev):
        r = {"seq": seq, "block_number": block, "transaction_index": txi, "tx_hash": h, "ts": ts,
             "touched_mask": mask, "n_events": n_ev}
        for i in range(n):
            r[f"sqrt_{i}"], r[f"liq_{i}"], r[f"tick_{i}"] = str(state[i]["sqrt"]), str(state[i]["liq"]), state[i]["tick"]
        return r

    rows = [row(0, window[0] - 1, -1, None, window_start_ts, 0, 0)]
    cur_key, mask, n_ev, cur = None, 0, 0, None
    for ev in events.iter_rows(named=True):
        key = (ev["block_number"], ev["transaction_index"])
        if key != cur_key and cur_key is not None:
            rows.append(row(len(rows), cur_key[0], cur_key[1], cur["tx_hash"], cur["block_timestamp"], mask, n_ev))
            mask, n_ev = 0, 0
        cur_key, cur = key, ev
        state[ev["pool_idx"]] = apply_event(state[ev["pool_idx"]], ev)
        mask |= 1 << ev["pool_idx"]
        n_ev += 1
    if cur_key is not None:
        rows.append(row(len(rows), cur_key[0], cur_key[1], cur["tx_hash"], cur["block_timestamp"], mask, n_ev))
    df = pl.DataFrame(rows, infer_schema_length=None)
    return df.with_columns(pl.col("block_number").alias("block_start"), pl.col("block_number").alias("block_end"),
                           pl.col("ts").alias("ts_start"))


def state_at_end_of_block(tx_states: pl.DataFrame, blocks: list[int]) -> dict[int, dict]:
    """Replayed state at the end of each block: the last transaction row at or before it."""
    bn = tx_states["block_number"].to_list()
    rows = tx_states.to_dicts()
    out = {}
    for b in blocks:
        k = bisect.bisect_right(bn, b) - 1
        if k < 0:
            raise ValueError(f"block {b} is before the seed state")
        out[b] = rows[k]
    return out


# ---------------------------------------------------------------- 4. per-pair rows and episodes


def pair_masks(metas: list[PoolMeta]) -> dict[str, int]:
    return {f"{metas[i].name}-{metas[j].name}": (1 << i) | (1 << j) for i, j in combinations(range(len(metas)), 2)}


def tx_pair_rows(tx_states: pl.DataFrame, metas: list[PoolMeta], registry: dict) -> pl.DataFrame:
    """Per pair, one row per transaction touching the pair's pools (and the seed row)."""
    rows = pair_rows(tx_states, metas, registry, carry=("seq", "block_number", "transaction_index", "tx_hash",
                                                        "touched_mask"))
    masks = pair_masks(metas)
    rows = rows.with_columns(pl.col("pair").replace_strict(masks, return_dtype=pl.Int64).alias("pair_mask"))
    return rows.filter((pl.col("seq") == 0) | ((pl.col("touched_mask") & pl.col("pair_mask")) != 0)).drop("n_blocks")


PEAK_COLS = ("threshold_bps", "cheap_pool", "expensive_pool", "size_usdc", "within_tick_range", "price_cheap",
             "price_expensive")


def find_tx_episodes(rows: pl.DataFrame, window: tuple[int, int], window_end_close_ts: int | None = None) -> pl.DataFrame:
    """Merge consecutive above-threshold transaction rows of each pair into episodes.

    `rows` needs: pair, pair_mask, seq, block_number, transaction_index, tx_hash, ts_start,
    touched_mask, above, gap_bps, gross_usdc (+ the PEAK_COLS, taken from the peak row).

    Per episode: opened by the transaction at start_*, closed by the transaction at close_*.
    - duration_txs: transactions touching the pair's pools from the opener (excluded) to the
      closer (included); 1 = the very next such transaction closed it. Right-censored: the
      number seen while open (a lower bound).
    - duration_blocks: close_block - start_block, so 0 = opened and closed inside one block.
      Right-censored: hi + 1 - start_block (a lower bound).
    - tx_index_distance: close - start transaction index, for same-block episodes only (counts
      every transaction in between, tracked or not).
    - duration_s: close_ts - start_ts (1 s timestamp resolution).
    - visible_at_block_level: still open at the end of at least one block.
    - closer_touches_both_pools: the closing transaction had events in both pools of the pair
      (typical of an arbitrage transaction).
    """
    lo, hi = window
    episodes = []
    for (pair,), g in rows.sort("seq").group_by(["pair"], maintain_order=True):
        r = g.to_dicts()
        k = 0
        while k < len(r):
            if not r[k]["above"]:
                k += 1
                continue
            run = [r[k]]
            while k + 1 < len(r) and r[k + 1]["above"]:
                k += 1
                run.append(r[k])
            nxt = r[k + 1] if k + 1 < len(r) else None
            first, peak = run[0], max(run, key=lambda s: (s["gross_usdc"], s["gap_bps"]))
            closed = nxt is not None
            close_block = nxt["block_number"] if closed else None
            close_ts = nxt["ts_start"] if closed else window_end_close_ts
            ep = {
                "pair": pair,
                "start_seq": first["seq"], "start_block": first["block_number"],
                "start_tx_index": first["transaction_index"], "start_tx_hash": first["tx_hash"],
                "start_ts": first["ts_start"],
                "close_seq": nxt["seq"] if closed else None, "close_block": close_block,
                "close_tx_index": nxt["transaction_index"] if closed else None,
                "close_tx_hash": nxt["tx_hash"] if closed else None, "close_ts": close_ts,
                "duration_txs": len(run) if closed else len(run) - 1,
                "duration_blocks": (close_block if closed else hi + 1) - first["block_number"],
                "tx_index_distance": (nxt["transaction_index"] - first["transaction_index"])
                if closed and close_block == first["block_number"] else None,
                "duration_s": (close_ts - first["ts_start"]) if close_ts is not None and first["ts_start"] is not None else None,
                "intra_block": closed and close_block == first["block_number"],
                "visible_at_block_level": (not closed) or close_block > first["block_number"],
                "closer_touches_both_pools": closed and (nxt["touched_mask"] & first["pair_mask"]) == first["pair_mask"],
                "left_censored": first["seq"] == 0,
                "right_censored": not closed,
                "peak_gap_bps": max(s["gap_bps"] for s in run),
                "peak_seq": peak["seq"], "peak_block": peak["block_number"],
                "peak_tx_index": peak["transaction_index"],
                "peak_gross_usdc": peak["gross_usdc"],
                **{c: peak[c] for c in PEAK_COLS if c in peak},
            }
            episodes.append(ep)
            k += 1
    return pl.DataFrame(episodes, infer_schema_length=None) if episodes else pl.DataFrame()


# ---------------------------------------------------------------- 5. block-level vs transaction-level


def open_at_block_end(tx_ep: dict, window: tuple[int, int]) -> range:
    """Blocks (inside the window) at whose end a transaction-level episode is open."""
    lo, hi = window
    start = max(tx_ep["start_block"], lo)
    end = hi if tx_ep["right_censored"] else tx_ep["close_block"] - 1
    return range(start, end + 1)


def compare_levels(tx_eps: pl.DataFrame, block_eps: pl.DataFrame, window: tuple[int, int]) -> tuple[pl.DataFrame, dict]:
    """Match transaction-level episodes to block-level episodes.

    Returns tx_eps with `block_episode_idx` (row index in block_eps, null = missed by the block
    level) and a summary dict. `consistent` is True when, for every pair, the set of blocks at
    whose end some transaction-level episode is open equals the union of block-level episode
    blocks: both replays read the same exact prices, so anything else is a bug.
    """
    if tx_eps.is_empty():
        return tx_eps, {"consistent": block_eps.is_empty(), "n_tx": 0, "n_block": block_eps.height, "n_missed": 0}
    b = block_eps.with_row_index("block_episode_idx").to_dicts() if not block_eps.is_empty() else []
    idx, consistent = [], True
    pairs = set(tx_eps["pair"].to_list()) | {e["pair"] for e in b}
    for pair in pairs:
        tx_blocks = set()
        for e in tx_eps.filter(pl.col("pair") == pair).iter_rows(named=True):
            tx_blocks.update(open_at_block_end(e, window))
        blk_blocks = {n for e in b if e["pair"] == pair for n in range(e["start_block"], e["end_block"] + 1)}
        consistent &= tx_blocks == blk_blocks
    for e in tx_eps.iter_rows(named=True):
        blocks = open_at_block_end(e, window)
        match = None
        if len(blocks):
            match = next((m["block_episode_idx"] for m in b
                          if m["pair"] == e["pair"] and m["start_block"] <= blocks[0] <= m["end_block"]), None)
        idx.append(match)
    tx_eps = tx_eps.with_columns(pl.Series("block_episode_idx", idx, dtype=pl.UInt32))
    vis = tx_eps.filter(pl.col("block_episode_idx").is_not_null())
    split = vis.group_by("block_episode_idx").len().filter(pl.col("len") > 1)
    summary = {
        "consistent": bool(consistent),
        "n_tx": tx_eps.height,
        "n_block": len(b),
        "n_missed": int(tx_eps["block_episode_idx"].is_null().sum()),
        "n_visible": vis.height,
        "n_block_matched": int(vis["block_episode_idx"].n_unique()) if vis.height else 0,
        "n_block_with_several_tx_episodes": split.height,
    }
    return tx_eps, summary


# ---------------------------------------------------------------- exact multi-tick sizing at the peak transaction


def size_tx_episodes(rpc, episodes: pl.DataFrame, tx_states: pl.DataFrame, events: pl.DataFrame,
                     metas: list[PoolMeta], cfg: dict) -> list[dict]:
    """Exact multi-tick sizing at each episode's peak transaction.

    Tick table: get_tick_snapshot at the end of block peak_block - 1 (the seed row is already an
    end-of-block state, at lo - 1). sqrtPriceX96, tick and liquidity are replaced by the replayed
    state after the peak transaction. mt_tick_table_exact is False when a Mint/Burn of one of
    the two pools happened in peak_block at or before the peak transaction (it could have
    changed an initialized tick, which this table would not show)."""
    from data.collectors.ticks import get_tick_snapshot
    from data.pricing.v3_swap import TickDataOutOfRange
    from research.replay.multitick import optimal_arb_exact

    index = {m.address: i for i, m in enumerate(metas)}
    by_meta = {m.address: m for m in metas}
    state_by_seq = {r["seq"]: r for r in tx_states.iter_rows(named=True)}
    liq_ev = events.filter(pl.col("kind") != "swap")
    out = []
    for ep in episodes.iter_rows(named=True):
        st = state_by_seq[ep["peak_seq"]]
        seed = ep["peak_seq"] == 0
        snap_block = st["block_number"] if seed else st["block_number"] - 1
        addrs = (ep["cheap_pool"], ep["expensive_pool"])
        words = {a: cfg["words_each_side"] for a in addrs}
        while True:
            snaps = {}
            for a in addrs:
                i = index[a]
                s = get_tick_snapshot(rpc, a, snap_block, words[a])
                snaps[a] = replace(s, sqrt_price_x96=int(st[f"sqrt_{i}"]), tick=st[f"tick_{i}"], liquidity=int(st[f"liq_{i}"]))
            try:
                res = optimal_arb_exact(snaps[addrs[0]], snaps[addrs[1]],
                                        by_meta[addrs[0]].weth_is_token0, by_meta[addrs[1]].weth_is_token0)
                break
            except TickDataOutOfRange:
                if max(words.values()) >= cfg["max_words_each_side"]:
                    raise
                words = {a: w + cfg["widen_step"] for a, w in words.items()}
        exact = seed or liq_ev.filter(
            pl.col("pool_idx").is_in([index[a] for a in addrs]) & (pl.col("block_number") == st["block_number"])
            & (pl.col("transaction_index") <= st["transaction_index"])).is_empty()
        trip = res["trip"]
        scale = 10 ** by_meta[addrs[0]].usdc_decimals
        out.append({
            "mt_size_usdc": res["size_raw"] / scale,
            "mt_gross_usdc": res["profit_raw"] / scale,
            "mt_ticks_crossed_cheap": len(trip.leg1.ticks_crossed) if trip else 0,
            "mt_ticks_crossed_expensive": len(trip.leg2.ticks_crossed) if trip and trip.leg2 else 0,
            "mt_tick_table_block": snap_block,
            "mt_tick_table_exact": exact,
        })
    return out


# ---------------------------------------------------------------- data quality


def checkpoint_blocks(window: tuple[int, int], every: int) -> list[int]:
    lo, hi = window
    blocks = list(range(lo - 1 + every, hi + 1, every))
    return blocks if blocks and blocks[-1] == hi else blocks + [hi]


def quality_checks(tx_states: pl.DataFrame, swaps: list[pl.DataFrame], initial: list[dict], pools: list[str],
                   blocks: list[int], fetch_state) -> pl.DataFrame:
    """Compare the replayed end-of-block state with archive slot0()/liquidity() at `blocks`.

    `fetch_state(pool, block)` returns {sqrt_price_x96, tick, liquidity}. One row per
    (block, pool), with match flags for the transaction-level state (Swap + Mint/Burn) and, for
    contrast, the swap-only liquidity used by the block-level replay (last Swap event's L)."""
    eob = state_at_end_of_block(tx_states, blocks)
    rows = []
    for i, p in enumerate(pools):
        sw = swaps[i].sort(["block_number", "log_index"])
        sw_blocks, sw_liq = sw["block_number"].to_list(), sw["liquidity"].to_list()
        for b in blocks:
            chain = fetch_state(p, b)
            r = eob[b]
            k = bisect.bisect_right(sw_blocks, b) - 1
            swap_only_liq = int(sw_liq[k]) if k >= 0 else int(initial[i]["liquidity"])
            rows.append({
                "block": b, "pool": p,
                "sqrt_match": int(r[f"sqrt_{i}"]) == int(chain["sqrt_price_x96"]),
                "tick_match": r[f"tick_{i}"] == int(chain["tick"]),
                "liq_match": int(r[f"liq_{i}"]) == int(chain["liquidity"]),
                "swap_only_liq_match": swap_only_liq == int(chain["liquidity"]),
                "liq_replay": r[f"liq_{i}"], "liq_chain": str(chain["liquidity"]),
            })
    return pl.DataFrame(rows).with_columns(
        (pl.col("sqrt_match") & pl.col("tick_match") & pl.col("liq_match")).alias("match"))
