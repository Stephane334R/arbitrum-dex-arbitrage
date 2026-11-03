"""Block-level replay of cross-pool price gaps, single-tick arbitrage sizing and episodes.

Method
------
1. State. A pool's sqrtPriceX96, tick and in-range liquidity at the end of block N are those
   of its last Swap at or before N (forward-filled), seeded with slot0()/liquidity() at the
   block before the window. Price is exact: in Uniswap v3 only swap() moves sqrtPriceX96.
   Liquidity is APPROXIMATE: Mint/Burn events between swaps also change in-range liquidity
   and are not replayed, so L is the value as of the pool's last Swap.
2. Segments. State only changes at blocks with a swap, so the window is split into segments
   [block_start, block_end] where every pool's state is constant. A segment counts as
   (block_end - block_start + 1) blocks in every per-block statistic.
3. Gap. For each pool pair, gap_bps = (p_hi / p_lo - 1) * 1e4, with p in USDC per WETH.
   A segment is above threshold when gap_bps > (fee_a + fee_b) in bps (the spec's
   sum-of-fees rule; the exact round-trip break-even is slightly higher, 1/((1-fa)(1-fb)) - 1,
   and is what the profit formula uses).
4. Gross profit, SINGLE-TICK UPPER BOUND. Each pool is treated as a constant-product pool on
   its virtual reserves in the current tick (x = L/sqrtP, y = L*sqrtP, raw units), fee
   charged on input. The arb is USDC -> WETH in the cheap pool -> USDC in the expensive pool,
   with closed-form optimal size. This is optimistic: it assumes perfect execution at the
   end-of-block state with no competition, and ignores liquidity changes when the trade
   crosses an initialized tick. `within_tick_range` reports whether the optimal trade stays
   inside the tickSpacing-aligned range around the current tick; when it does, the
   constant-product math is exact for the given L.
5. Episodes. Consecutive above-threshold segments of the same pair form one episode.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations

import polars as pl

from data.pricing.v3_price import usdc_per_weth

Q96 = 2**96


@dataclass(frozen=True)
class PoolMeta:
    address: str
    fee: int  # hundredths of a bip (500 = 0.05%)
    tick_spacing: int
    weth_is_token0: bool
    weth_decimals: int
    usdc_decimals: int

    @property
    def fee_bps(self) -> float:
        return self.fee / 100

    @property
    def gamma(self) -> float:
        return 1 - self.fee / 1_000_000


def pool_metas(registry: dict) -> list[PoolMeta]:
    weth = registry["tokens"]["WETH"]["address"].lower()
    out = []
    for p in registry["pools"]:
        w0 = p["token0"]["address"].lower() == weth
        out.append(
            PoolMeta(
                address=p["address"],
                fee=p["fee"],
                tick_spacing=p["tick_spacing"],
                weth_is_token0=w0,
                weth_decimals=(p["token0"] if w0 else p["token1"])["decimals"],
                usdc_decimals=(p["token1"] if w0 else p["token0"])["decimals"],
            )
        )
    return out


# ---------------------------------------------------------------- 1-2. state segments


def build_segments(
    swaps_by_pool: dict[str, pl.DataFrame], initial: dict[str, dict], window: tuple[int, int], window_start_ts: int
) -> pl.DataFrame:
    """One row per segment of constant state. Columns: block_start, block_end, ts_start,
    and per pool i (index in `swaps_by_pool` order): sqrt_i (str), liq_i (str), tick_i."""
    lo, hi = window
    pools = list(swaps_by_pool)
    last_per_block = {}
    for p, df in swaps_by_pool.items():
        d = df.filter(pl.col("block_number").is_between(lo, hi)).sort(["block_number", "log_index"])
        last_per_block[p] = {
            r["block_number"]: r for r in d.group_by("block_number", maintain_order=True).last().iter_rows(named=True)
        }
    ts_by_block = {b: r["block_timestamp"] for p in pools for b, r in last_per_block[p].items()}
    change_blocks = sorted({lo} | {b for p in pools for b in last_per_block[p]})

    state = {p: initial[p] for p in pools}
    rows = []
    for k, b in enumerate(change_blocks):
        for p in pools:
            if b in last_per_block[p]:
                state[p] = last_per_block[p][b]
        row = {
            "block_start": b,
            "block_end": (change_blocks[k + 1] - 1) if k + 1 < len(change_blocks) else hi,
            "ts_start": ts_by_block.get(b, window_start_ts if b == lo else None),
        }
        for i, p in enumerate(pools):
            row[f"sqrt_{i}"] = str(state[p]["sqrt_price_x96"])
            row[f"liq_{i}"] = str(state[p]["liquidity"])
            row[f"tick_{i}"] = int(state[p]["tick"])
        rows.append(row)
    return pl.DataFrame(rows, infer_schema_length=None)


# ---------------------------------------------------------------- 4. single-tick sizing


def _virtual_reserves(sqrt_price_x96: int, liquidity: int, m: PoolMeta) -> tuple[float, float]:
    """(WETH raw, USDC raw) virtual reserves in the current tick: x0 = L/sqrtP, x1 = L*sqrtP."""
    s = sqrt_price_x96 / Q96
    r0, r1 = liquidity / s, liquidity * s
    return (r0, r1) if m.weth_is_token0 else (r1, r0)


def _sqrt_after_input(sqrt_price_x96: int, liquidity: int, amount_in_raw: float, token_in_is_0: bool) -> float:
    """Raw sqrtP after adding `amount_in_raw` (already net of fee) within one tick range."""
    s = sqrt_price_x96 / Q96
    if token_in_is_0:  # 1/sqrtP' = 1/sqrtP + dx/L  (price of token0 falls)
        return 1 / (1 / s + amount_in_raw / liquidity)
    return s + amount_in_raw / liquidity  # sqrtP' = sqrtP + dy/L  (price rises)


def _stays_in_range(tick: int, spacing: int, sqrt_new: float, up: bool) -> bool:
    """Whether raw sqrtP can move to `sqrt_new` without reaching the next tick that could be
    initialized (a multiple of tickSpacing). Liquidity is constant until then."""
    base = math.floor(tick / spacing) * spacing
    bound_tick = base + spacing if up else base
    bound = 1.0001 ** (bound_tick / 2)
    return sqrt_new < bound if up else sqrt_new >= bound


def optimal_arb(cheap: dict, expensive: dict, mc: PoolMeta, me: PoolMeta) -> dict:
    """Optimal USDC-in arbitrage: buy WETH in `cheap`, sell it in `expensive`.

    Chaining two constant-product swaps gives out(d) = a*d / (b + c*d) with
        a = gA*gB*wA*uB,  b = wB*uA,  c = gA*(wB + gB*wA)
    (w = WETH, u = USDC virtual reserves, g = 1 - fee). Profit out(d) - d is maximised at
        d* = (sqrt(a*b) - b) / c,  profit* = (sqrt(a) - sqrt(b))**2 / c,
    and is positive only if a > b, i.e. gA*gB*p_exp/p_cheap > 1.
    """
    wA, uA = _virtual_reserves(int(cheap["sqrt"]), int(cheap["liq"]), mc)
    wB, uB = _virtual_reserves(int(expensive["sqrt"]), int(expensive["liq"]), me)
    gA, gB = mc.gamma, me.gamma
    a, b, c = gA * gB * wA * uB, wB * uA, gA * (wB + gB * wA)
    if a <= b or int(cheap["liq"]) == 0 or int(expensive["liq"]) == 0:
        return {"size_usdc": 0.0, "gross_usdc": 0.0, "within_tick_range": None}
    d = (math.sqrt(a * b) - b) / c
    profit = (math.sqrt(a) - math.sqrt(b)) ** 2 / c
    weth_out = wA * gA * d / (uA + gA * d)

    # Tick-range check: USDC into cheap pool, WETH into expensive pool.
    usdc_is_0_c = not mc.weth_is_token0
    s_c = _sqrt_after_input(int(cheap["sqrt"]), int(cheap["liq"]), gA * d, usdc_is_0_c)
    s_e = _sqrt_after_input(int(expensive["sqrt"]), int(expensive["liq"]), gB * weth_out, me.weth_is_token0)
    in_c = _stays_in_range(cheap["tick"], mc.tick_spacing, s_c, up=not usdc_is_0_c)
    in_e = _stays_in_range(expensive["tick"], me.tick_spacing, s_e, up=not me.weth_is_token0)

    scale = 10**mc.usdc_decimals
    return {"size_usdc": d / scale, "gross_usdc": profit / scale, "within_tick_range": in_c and in_e}


# ---------------------------------------------------------------- 3. per-pair gaps


def pair_rows(segments: pl.DataFrame, metas: list[PoolMeta], registry: dict) -> pl.DataFrame:
    """Per segment and pool pair: prices, gap, threshold, direction and single-tick gross."""
    pool_cfg = {p["address"]: p for p in registry["pools"]}
    out = []
    for seg in segments.iter_rows(named=True):
        prices = [float(usdc_per_weth(int(seg[f"sqrt_{i}"]), pool_cfg[m.address], registry)) for i, m in enumerate(metas)]
        for i, j in combinations(range(len(metas)), 2):
            lo_i, hi_i = (i, j) if prices[i] <= prices[j] else (j, i)
            gap = (prices[hi_i] / prices[lo_i] - 1) * 1e4
            threshold = metas[i].fee_bps + metas[j].fee_bps
            above = gap > threshold
            st = lambda k: {"sqrt": seg[f"sqrt_{k}"], "liq": seg[f"liq_{k}"], "tick": seg[f"tick_{k}"]}
            arb = optimal_arb(st(lo_i), st(hi_i), metas[lo_i], metas[hi_i]) if above else {
                "size_usdc": 0.0, "gross_usdc": 0.0, "within_tick_range": None}
            out.append({
                "pair": f"{metas[i].fee}-{metas[j].fee}",
                "block_start": seg["block_start"],
                "block_end": seg["block_end"],
                "n_blocks": seg["block_end"] - seg["block_start"] + 1,
                "ts_start": seg["ts_start"],
                "price_cheap": prices[lo_i],
                "price_expensive": prices[hi_i],
                "cheap_pool": metas[lo_i].address,
                "expensive_pool": metas[hi_i].address,
                "gap_bps": gap,
                "threshold_bps": threshold,
                "above": above,
                **arb,
            })
    return pl.DataFrame(out, infer_schema_length=None)


# ---------------------------------------------------------------- 5. episodes


def find_episodes(rows: pl.DataFrame, window: tuple[int, int], window_end_close_ts: int | None = None) -> pl.DataFrame:
    """Merge consecutive above-threshold segments of each pair into episodes.

    `rows` needs: pair, block_start, block_end, ts_start, above, gap_bps, gross_usdc (+ any
    per-segment columns such as net_usdc, which are taken from the peak-gross segment).
    Segments of a pair must tile the window contiguously.

    An episode is open at the end of blocks start..end and closed at the end of
    close_block = end + 1, which is the start of the next segment (a block with a swap, so its
    timestamp is known). duration_blocks = end - start + 1;
    duration_s = ts(close_block) - ts(start). Episodes still open at the window end are
    right-censored (close_block/duration_s use `window_end_close_ts` if given, else null);
    episodes already open at the window start are left-censored.
    """
    lo, hi = window
    episodes = []
    for (pair,), g in rows.sort("block_start").group_by(["pair"], maintain_order=True):
        segs = g.to_dicts()
        k = 0
        while k < len(segs):
            if not segs[k]["above"]:
                k += 1
                continue
            run = [segs[k]]
            while k + 1 < len(segs) and segs[k + 1]["above"]:
                k += 1
                run.append(segs[k])
            nxt = segs[k + 1] if k + 1 < len(segs) else None
            peak = max(run, key=lambda s: (s["gross_usdc"], s["gap_bps"]))
            start, end = run[0]["block_start"], run[-1]["block_end"]
            close_ts = nxt["ts_start"] if nxt is not None else window_end_close_ts
            ep = {
                "pair": pair,
                "start_block": start,
                "end_block": end,
                "close_block": end + 1,
                "duration_blocks": end - start + 1,
                "start_ts": run[0]["ts_start"],
                "close_ts": close_ts,
                "duration_s": (close_ts - run[0]["ts_start"]) if close_ts is not None and run[0]["ts_start"] is not None else None,
                "left_censored": start == lo,
                "right_censored": nxt is None,
                "n_segments": len(run),
                "peak_gap_bps": max(s["gap_bps"] for s in run),
                "peak_block": peak["block_start"],
                "peak_gross_usdc": peak["gross_usdc"],
            }
            for col in ("threshold_bps", "cheap_pool", "expensive_pool", "size_usdc", "within_tick_range",
                        "price_cheap", "price_expensive", "gas_units", "base_fee_wei", "gas_usdc", "net_usdc"):
                if col in peak:
                    ep[col if col not in ("net_usdc", "gas_usdc") else f"peak_{col}"] = peak[col]
            episodes.append(ep)
            k += 1
    return pl.DataFrame(episodes, infer_schema_length=None) if episodes else pl.DataFrame()


# ---------------------------------------------------------------- 6. metrics


def headline_metrics(segments: pl.DataFrame, episodes: pl.DataFrame, n_swaps: int, window_ts: tuple[int, int],
                     window: tuple[int, int]) -> dict:
    lo, hi = window
    m = {
        "n_blocks": hi - lo + 1,
        "hours": (window_ts[1] - window_ts[0]) / 3600,
        "n_swaps": n_swaps,
        "n_segments": segments.height,
        "gross_episodes": episodes.height,
    }
    if episodes.is_empty():
        return m | {"net_positive_episodes": 0, "pct_closed_within_1_block": None, "median_duration_blocks": None,
                    "p90_duration_blocks": None, "median_duration_s": None, "p90_duration_s": None,
                    "total_gross_usdc": 0.0, "total_net_usdc": 0.0, "censored_episodes": 0}
    closed = episodes.filter(~pl.col("right_censored"))
    pos = episodes.filter(pl.col("peak_net_usdc") > 0)
    q = lambda col, p: float(closed[col].quantile(p, "nearest")) if closed.height else None
    return m | {
        "net_positive_episodes": pos.height,
        "censored_episodes": int((episodes["left_censored"] | episodes["right_censored"]).sum()),
        "pct_closed_within_1_block": 100 * float((closed["duration_blocks"] == 1).mean()) if closed.height else None,
        "median_duration_blocks": q("duration_blocks", 0.5),
        "p90_duration_blocks": q("duration_blocks", 0.9),
        "median_duration_s": q("duration_s", 0.5),
        "p90_duration_s": q("duration_s", 0.9),
        "total_gross_usdc": float(episodes["peak_gross_usdc"].sum()),
        "total_net_usdc": float(pos["peak_net_usdc"].sum()),
        "pct_within_tick_range": 100 * float(episodes["within_tick_range"].fill_null(False).mean()),
    }


def blocks_weighted_gaps(rows: pl.DataFrame) -> pl.DataFrame:
    """Per pair: gap and the number of blocks it held (for per-block distributions)."""
    return rows.select("pair", "gap_bps", "threshold_bps", "n_blocks")


def exact_breakeven_bps(fee_a: int, fee_b: int) -> float:
    """Exact round-trip break-even gap: 1/((1-fa)(1-fb)) - 1, in bps."""
    g = Fraction(1_000_000 - fee_a, 1_000_000) * Fraction(1_000_000 - fee_b, 1_000_000)
    return float((1 / g - 1) * 10_000)
