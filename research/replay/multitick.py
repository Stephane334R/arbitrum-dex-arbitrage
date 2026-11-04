"""Exact multi-tick sizing of a two-pool arbitrage, using the swap port in data/pricing/v3_swap.py.

The arbitrage is the same as the single-tick sizing in replay.py: USDC in -> WETH in the cheap
pool -> USDC in the expensive pool. Here every leg runs through the exact Uniswap v3 swap loop
on a tick snapshot read from the chain at the episode's peak block
(data/collectors/ticks.py). So liquidity changes at initialized ticks are applied, liquidity
is the real on-chain value at that block (Mint/Burn included), and all rounding is the pool's.

Optimal size: profit(d) = usdc_out(d) - usdc_in(d), over integer d in raw USDC units.
In exact arithmetic profit is concave: each leg's output is concave in its input, because the
marginal price only gets worse as the price moves. The search uses that:
  1. evaluate d = 2^k (k = 0, 1, ...) until profit falls (only checked from d = 2^20, about
     1 USDC, because rounding noise dominates below that);
  2. ternary search on [2^(k*-1), 2^(k*+1)] down to a 3-unit bracket;
  3. return the best d among *all* points evaluated.
APPROXIMATION (labelled): integer rounding makes profit non-concave at the scale of a few raw
units (a few 1e-6 USDC), so the size found can differ from the exact integer optimum by a few
units. The effect on profit is of the same order, far below anything reported.
"""

from __future__ import annotations

from dataclasses import dataclass

from data.pricing.v3_swap import PoolSnapshot, SolidityRevert, SwapResult, swap

MAX_USDC_RAW = 10**14  # 100M USDC at 6 decimals: an upper bound for the doubling search
# Below ~1 USDC (2^20 raw units), wei-level rounding can make profit dip and recover, so the
# doubling search does not stop on a decrease before that size.
NOISE_FLOOR_LOG2 = 20


@dataclass
class RoundTrip:
    usdc_in: int  # raw USDC actually consumed by leg 1 (can be < requested at the price limit)
    weth_mid: int  # raw WETH out of leg 1 = into leg 2
    usdc_out: int
    leg1: SwapResult
    leg2: SwapResult | None

    @property
    def profit(self) -> int:
        return self.usdc_out - self.usdc_in


def round_trip(cheap: PoolSnapshot, expensive: PoolSnapshot, cheap_weth_is_0: bool, exp_weth_is_0: bool,
               usdc_in: int) -> RoundTrip | None:
    """Exact-input USDC -> WETH in `cheap`, then all that WETH -> USDC in `expensive`.
    Returns None if either leg would revert (e.g. no liquidity left). Raises
    TickDataOutOfRange if a snapshot is too narrow: widen it and retry."""
    try:
        leg1 = swap(cheap, zero_for_one=not cheap_weth_is_0, amount_specified=usdc_in)
        if leg1.amount_out <= 0:
            return RoundTrip(leg1.amount_in, 0, 0, leg1, None)
        leg2 = swap(expensive, zero_for_one=exp_weth_is_0, amount_specified=leg1.amount_out)
    except SolidityRevert:
        return None
    return RoundTrip(leg1.amount_in, leg1.amount_out, leg2.amount_out, leg1, leg2)


def optimal_arb_exact(cheap: PoolSnapshot, expensive: PoolSnapshot, cheap_weth_is_0: bool, exp_weth_is_0: bool,
                      max_usdc_raw: int = MAX_USDC_RAW) -> dict:
    """Profit-maximising USDC input. Returns raw integers plus the best RoundTrip (None if no
    size is profitable, in which case size and profit are 0)."""
    memo: dict[int, RoundTrip | None] = {}

    def f(d: int) -> int | None:
        if d not in memo:
            memo[d] = round_trip(cheap, expensive, cheap_weth_is_0, exp_weth_is_0, d)
        rt = memo[d]
        return None if rt is None else rt.profit

    # 1. doubling
    k_best, p_best, k = 0, f(1), 1
    while 2**k <= max_usdc_raw:
        p = f(2**k)
        if p is None:
            break
        if p_best is None or p > p_best:
            k_best, p_best = k, p
        elif p < p_best and k >= NOISE_FLOOR_LOG2:
            break  # past the peak, or never profitable: profit only falls from here
        k += 1

    # 2. ternary search around the best power of two
    if p_best is not None and p_best > 0:
        lo, hi = max(1, 2 ** (k_best - 1)), min(max_usdc_raw, 2 ** (k_best + 1))
        while hi - lo > 2:
            m1, m2 = lo + (hi - lo) // 3, hi - (hi - lo) // 3
            p1, p2 = f(m1), f(m2)
            if p2 is None or (p1 is not None and p1 >= p2):
                hi = m2
            else:
                lo = m1
        for d in range(lo, hi + 1):
            f(d)

    # 3. best evaluated point
    valid = [(rt.profit, d) for d, rt in memo.items() if rt is not None]
    best_profit, best_d = max(valid) if valid else (0, 0)
    if best_profit <= 0:
        return {"size_raw": 0, "profit_raw": 0, "trip": None, "evaluations": len(memo)}
    return {"size_raw": memo[best_d].usdc_in, "profit_raw": best_profit, "trip": memo[best_d], "evaluations": len(memo)}


# --- per-episode sizing (reads the chain through the tick collector's Parquet cache) ------------


def size_episodes(rpc, episodes, segments, metas, cfg: dict) -> list[dict]:
    """For each episode, exact multi-tick sizing at its peak block. Returns one dict per
    episode (same order), to be added as columns:

    - mt_size_usdc / mt_gross_usdc: exact multi-tick optimum on the on-chain state.
    - st_onchain_l_gross_usdc: the single-tick formula again, but with the on-chain liquidity
      at the peak block instead of the replay's forward-filled value. It splits the
      single-tick error into (a) stale liquidity and (b) ignoring tick crossings.
    - price_matches_replay / liquidity_matches_replay: whether slot0 at the peak block equals
      the replay's state. Price must match (only swaps move it). Liquidity can differ when a
      Mint/Burn changed in-range liquidity after the pool's last swap.
    """
    from data.collectors.ticks import get_tick_snapshot
    from data.pricing.v3_swap import TickDataOutOfRange
    from research.replay.replay import optimal_arb

    index = {m.address: i for i, m in enumerate(metas)}
    by_meta = {m.address: m for m in metas}
    seg_at = {r["block_start"]: r for r in segments.iter_rows(named=True)}
    out = []
    for ep in episodes.iter_rows(named=True):
        block, seg = ep["peak_block"], seg_at[ep["peak_block"]]
        addrs = (ep["cheap_pool"], ep["expensive_pool"])
        words = {a: cfg["words_each_side"] for a in addrs}
        while True:
            snaps = {a: get_tick_snapshot(rpc, a, block, words[a]) for a in addrs}
            try:
                res = optimal_arb_exact(snaps[addrs[0]], snaps[addrs[1]],
                                        by_meta[addrs[0]].weth_is_token0, by_meta[addrs[1]].weth_is_token0)
                break
            except TickDataOutOfRange:
                if max(words.values()) >= cfg["max_words_each_side"]:
                    raise
                words = {a: w + cfg["widen_step"] for a, w in words.items()}

        replay_state = {a: (int(seg[f"sqrt_{index[a]}"]), int(seg[f"liq_{index[a]}"])) for a in addrs}
        price_ok = all(snaps[a].sqrt_price_x96 == replay_state[a][0] for a in addrs)
        if not price_ok:
            raise RuntimeError(f"episode at block {block}: on-chain sqrtPriceX96 differs from the replay state")
        st = lambda a: {"sqrt": str(snaps[a].sqrt_price_x96), "liq": str(snaps[a].liquidity), "tick": snaps[a].tick}
        st_onchain = optimal_arb(st(addrs[0]), st(addrs[1]), by_meta[addrs[0]], by_meta[addrs[1]])
        trip = res["trip"]
        scale = 10 ** by_meta[addrs[0]].usdc_decimals
        out.append({
            "mt_size_usdc": res["size_raw"] / scale,
            "mt_gross_usdc": res["profit_raw"] / scale,
            "mt_gross_raw": res["profit_raw"],
            "mt_ticks_crossed_cheap": len(trip.leg1.ticks_crossed) if trip else 0,
            "mt_ticks_crossed_expensive": len(trip.leg2.ticks_crossed) if trip and trip.leg2 else 0,
            "mt_evaluations": res["evaluations"],
            "st_onchain_l_gross_usdc": st_onchain["gross_usdc"],
            "price_matches_replay": price_ok,
            "liquidity_matches_replay": all(snaps[a].liquidity == replay_state[a][1] for a in addrs),
            "tick_words_each_side": max(words.values()),
        })
    return out
