"""CLI: replay cached swaps, find arbitrage episodes, save results.

Usage:  uv run python -m research.replay.run

Outputs research/results/<run_id>/: summary.md, episodes.parquet, config.json, and charts
(gap_distribution.png, episode_durations.png, largest_episode_prices.png).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

import polars as pl
import yaml

from data.collectors.blocks import get_headers
from data.collectors.pool_state import get_pool_state
from data.collectors.swaps import covered_ranges, load_swaps, missing_ranges, pool_cache_dir
from data.pools.registry import load_registry
from data.pricing.v3_price import usdc_per_weth
from data.rpc import RpcClient, get_web3, load_rpc_url, provider_name, redact
from research.analytics import charts
from research.replay.gas import gas_for_pairs
from research.replay.multitick import size_episodes
from research.replay.replay import (
    build_segments,
    exact_breakeven_bps,
    find_episodes,
    headline_metrics,
    pair_rows,
    pool_metas,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "configs" / "replay.yaml"
RESULTS_ROOT = ROOT / "research" / "results"


def git_info() -> dict:
    def run(*a):
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()

    return {"commit": run("rev-parse", "HEAD"), "dirty": bool(run("status", "--porcelain", "--", ".", ":!research/results"))}


def common_window(pools: list[str], cfg: dict) -> tuple[int, int]:
    """Default window: the last `blocks` blocks of the range cached for every pool."""
    ends = []
    for p in pools:
        cov = covered_ranges(pool_cache_dir(p))
        if not cov:
            raise RuntimeError(f"no cached swaps for {p}; run `python -m data.collectors.swaps`")
        ends.append(cov[-1][1])
    hi = cfg["to_block"] or min(ends)
    lo = cfg["from_block"] or hi - cfg["blocks"] + 1
    for p in pools:  # every block of the window must be cached for every pool
        gaps = missing_ranges((lo, hi), covered_ranges(pool_cache_dir(p)))
        if gaps:
            raise RuntimeError(f"pool {p} has uncached ranges {gaps[:3]}; run the collector first")
    return lo, hi


def main() -> int:
    url = load_rpc_url()
    try:
        return run(url)
    except Exception as e:  # never let the RPC URL leak through an error message
        print(f"replay failed: {redact(e, url)}", file=sys.stderr)
        return 1


def run(url: str) -> int:
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    registry = load_registry()
    metas = pool_metas(registry)
    pools = [m.address for m in metas]
    fees = [m.fee for m in metas]
    client, w3 = RpcClient(url), get_web3(url)

    lo, hi = common_window(pools, cfg)
    print(f"window {lo}..{hi} ({hi - lo + 1} blocks)")
    swaps = {p: load_swaps(p, (lo, hi)) for p in pools}
    n_swaps = sum(df.height for df in swaps.values())
    initial = {p: get_pool_state(w3, p, lo - 1) for p in pools}
    hdr = {r["block_number"]: r for r in get_headers(client, [lo, hi]).iter_rows(named=True)}
    window_ts = (hdr[lo]["timestamp"], hdr[hi]["timestamp"])

    segments = build_segments(swaps, initial, (lo, hi), window_ts[0])
    rows = pair_rows(segments, metas, registry)
    print(f"{n_swaps} swaps, {segments.height} constant-state segments, {rows.height} pair rows")

    # Episodes; right-censored durations are measured to the window end (a lower bound).
    episodes = find_episodes(rows, (lo, hi), window_end_close_ts=window_ts[1])

    # Exact multi-tick sizing at each episode's peak block. The single-tick numbers are kept as
    # *_single_tick comparison columns; peak_gross_usdc / size_usdc become the multi-tick values.
    if not episodes.is_empty():
        mt = pl.DataFrame(size_episodes(client, episodes, segments, metas, cfg["multitick"]))
        episodes = (
            pl.concat([episodes, mt], how="horizontal")
            .rename({"peak_gross_usdc": "peak_gross_usdc_single_tick", "size_usdc": "size_usdc_single_tick"})
            .with_columns(pl.col("mt_gross_usdc").alias("peak_gross_usdc"), pl.col("mt_size_usdc").alias("size_usdc"))
        )
        print(f"multi-tick sizing: {episodes.height} episodes, single-tick gross "
              f"${episodes['peak_gross_usdc_single_tick'].sum():.4f} vs exact ${episodes['peak_gross_usdc'].sum():.4f}")

    pairs = [(a.fee, b.fee) for a, b in combinations(metas, 2)]
    gas = gas_for_pairs(client, cfg, registry, pairs, hi)
    print(f"gas: {gas['label']}")
    if not episodes.is_empty():
        peaks = get_headers(client, episodes["peak_block"].unique().to_list())
        episodes = (
            episodes.join(peaks.select(pl.col("block_number").alias("peak_block"), pl.col("base_fee_per_gas").alias("base_fee_wei")),
                          on="peak_block", how="left")
            .with_columns(pl.col("pair").replace_strict({k: v["gas_units"] for k, v in gas["per_pair"].items()},
                                                        return_dtype=pl.Int64).alias("gas_units"))
            .with_columns(
                (pl.col("gas_units") * pl.col("base_fee_wei") / 1e18
                 * (pl.col("price_cheap") + pl.col("price_expensive")) / 2 * cfg["usdc_usd"]).alias("peak_gas_usdc"))
            .with_columns((pl.col("peak_gross_usdc") - pl.col("peak_gas_usdc")).alias("peak_net_usdc"),
                          (pl.col("peak_gross_usdc_single_tick") - pl.col("peak_gas_usdc")).alias("peak_net_usdc_single_tick"))
        )

    metrics = headline_metrics(segments, episodes, n_swaps, window_ts, (lo, hi))

    # ---- save
    git = git_info()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + (git["commit"][:7] or "nogit")
    out = RESULTS_ROOT / run_id
    out.mkdir(parents=True)
    episodes.write_parquet(out / "episodes.parquet")
    config = {
        "run_id": run_id,
        "block_range": [lo, hi],
        "block_timestamps": list(window_ts),
        "chain_id": registry["chain_id"],
        "registry_verified_at_block": registry["verified_at_block"],
        "pools": [{"address": m.address, "fee": m.fee, "tick_spacing": m.tick_spacing} for m in metas],
        "thresholds_bps": {f"{a}-{b}": {"sum_of_fees": (a + b) / 100, "exact_breakeven": exact_breakeven_bps(a, b)}
                           for a, b in pairs},
        "gas_assumption": gas,
        "usdc_usd": cfg["usdc_usd"],
        "rpc_provider": provider_name(url),
        "git_commit": git["commit"],
        "git_dirty": git["dirty"],
        "config": cfg,
        "simplifications": SIMPLIFICATIONS,
    }
    (out / "config.json").write_text(json.dumps(config, indent=2))

    charts.gap_distribution(rows, out / "gap_distribution.png")
    if not episodes.is_empty():
        charts.duration_histogram(episodes, out / "episode_durations.png")
        largest = episodes.sort("peak_gross_usdc", descending=True).row(0, named=True)
        pool_cfg = {p["address"]: p for p in registry["pools"]}
        price_rows = segments.select(
            "block_start",
            *[pl.col(f"sqrt_{i}").map_elements(lambda s, a=m.address: float(usdc_per_weth(int(s), pool_cfg[a], registry)),
                                               return_dtype=pl.Float64).alias(f"p_{m.fee}") for i, m in enumerate(metas)])
        charts.episode_prices(price_rows, largest, fees, out / "largest_episode_prices.png")

    (out / "summary.md").write_text(render_summary(run_id, metrics, episodes, gas, config, (lo, hi)))
    print(f"saved {out.relative_to(ROOT)}")
    print((out / "summary.md").read_text())
    return 0


SIMPLIFICATIONS = [
    "Gross profit is the EXACT multi-tick optimum at the episode's peak block: both legs run through the "
    "Uniswap v3 swap port (data/pricing/v3_swap.py, QuoterV2-parity tested) on the on-chain tick state at that "
    "block; optimal size by integer search (within a few raw units of the true optimum). It still assumes "
    "perfect execution at the end-of-block state and no competition.",
    "The peak block is chosen by the SINGLE-TICK gross (constant product on the current tick's virtual "
    "reserves), as in the first run; the single-tick numbers are kept as *_single_tick comparison columns.",
    "Gap detection uses in-range liquidity forward-filled from Swap events (Mint/Burn between swaps are not "
    "replayed); the multi-tick sizing reads the real on-chain liquidity at the peak block instead.",
    "Gap threshold is the sum of the two fees (spec); the exact break-even 1/((1-fa)(1-fb))-1 is slightly higher "
    "and is what the profit formula uses, so a few gross episodes can have ~0 profit.",
    "States are end-of-block; intra-block gaps that were opened and closed within one block are invisible.",
    "Pairs are evaluated independently; simultaneous episodes on pairs sharing a pool compete for the same "
    "liquidity, so summed profits overstate what one arbitrageur could capture.",
    "Gas: one representative estimate per pair (see gas_assumption), priced at each episode's peak-block base fee.",
    "Profits in USDC with 1 USDC = 1 USD.",
]


def single_vs_multi_section(episodes: pl.DataFrame) -> list[str]:
    """How much the single-tick approximation overstated gross profit, split into three causes:
    stale liquidity (replay L vs on-chain L at the peak block), initialized ticks crossed by the
    exact optimal trade, and integer rounding (episodes whose exact trade crosses no tick)."""
    e = episodes.with_columns(
        ((pl.col("mt_ticks_crossed_cheap") + pl.col("mt_ticks_crossed_expensive")) > 0).alias("_crosses"))
    st = float(e["peak_gross_usdc_single_tick"].sum())
    st_l = float(e["st_onchain_l_gross_usdc"].sum())
    mt = float(e["peak_gross_usdc"].sum())
    over = st - mt
    diff = lambda d: float((d["st_onchain_l_gross_usdc"] - d["peak_gross_usdc"]).sum())
    crossing, flat = e.filter(pl.col("_crosses")), e.filter(~pl.col("_crosses"))
    pct = f"{100 * over / mt:,.2f}% of the exact value" if mt > 0 else "the exact value is 0"
    n_liq = int((~e["liquidity_matches_replay"]).sum())
    lines = [
        "", "## Single-tick vs exact multi-tick", "",
        f"Summed over the {e.height} episodes, the single-tick approximation gave **${st:,.6f}** gross; the exact "
        f"multi-tick simulation at the same peak blocks gives **${mt:,.6f}**. The single-tick number "
        f"{'overstated' if over > 0 else 'understated'} gross profit by **${abs(over):,.6f}** ({pct}).",
        "",
        f"- Stale liquidity: ${st - st_l:,.6f}. The single-tick formula re-run with the on-chain liquidity at the "
        f"peak block gives ${st_l:,.6f} ({n_liq} of {e.height} episodes had in-range liquidity changed by a "
        f"Mint/Burn since the pool's last swap).",
        f"- Tick crossings: ${diff(crossing):,.6f}, from the {crossing.height} episode(s) whose exact optimal trade "
        f"crosses at least one initialized tick.",
        f"- Integer rounding: ${diff(flat):,.6f} over the other {flat.height} episodes (the exact profit is a whole "
        f"number of raw USDC units, 1e-6 USDC; the single-tick formula is continuous).",
        "",
        "| Pair | Peak block | Gap (bps) | Single-tick size / gross (USD) | Single-tick, on-chain L, gross (USD) | "
        "Multi-tick size / gross (USD) | Ticks crossed (cheap, exp.) | L matches replay |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in e.sort("peak_block").iter_rows(named=True):
        lines.append(
            f"| {r['pair']} | {r['peak_block']} | {r['peak_gap_bps']:.2f} | {r['size_usdc_single_tick']:,.2f} / "
            f"{r['peak_gross_usdc_single_tick']:.6f} | {r['st_onchain_l_gross_usdc']:.6f} | {r['size_usdc']:,.2f} / "
            f"{r['peak_gross_usdc']:.6f} | {r['mt_ticks_crossed_cheap']}, {r['mt_ticks_crossed_expensive']} | "
            f"{'yes' if r['liquidity_matches_replay'] else 'no'} |")
    lines += ["", "Sizes differ more than profits because profit is flat near the optimum."]
    return lines


def _fmt(v, spec=",.2f", none="n/a"):
    return none if v is None else format(v, spec)


def render_summary(run_id: str, m: dict, episodes: pl.DataFrame, gas: dict, config: dict, window) -> str:
    G, M = m["gross_episodes"], m["net_positive_episodes"]
    pct1 = m.get("pct_closed_within_1_block")
    if M:
        net_clause = f"${m['total_net_usdc']:,.2f} net of gas over the {M:,} net-positive episodes"
    elif G:
        net_clause = f"none is profitable after gas (mean ${episodes['peak_gas_usdc'].mean():.3f} per arbitrage)"
    else:
        net_clause = "no episodes to execute"
    headline = (
        f"Over {m['hours']:.1f} hours ({m['n_blocks']:,} blocks, {m['n_swaps']:,} swaps), the WETH/USDC pools showed "
        f"{G:,} episodes with a gap above the fee threshold, {_fmt(pct1, '.0f')}% of closed episodes lasted one block, "
        f"and their exact multi-tick gross profit sums to ${m['total_gross_usdc']:,.4f}; {net_clause}."
    )
    table = [
        ("Blocks (N)", f"{m['n_blocks']:,} ({window[0]}..{window[1]})"),
        ("Hours (H)", f"{m['hours']:.2f}"),
        ("Swaps (S)", f"{m['n_swaps']:,}"),
        ("Gross episodes (G): gap > sum of fees", f"{G:,}"),
        ("Net-positive episodes (M)", f"{M:,}"),
        ("Closed within 1 block (P)", f"{_fmt(pct1, '.1f')}% of closed episodes"),
        ("Median / p90 duration (blocks)", f"{_fmt(m.get('median_duration_blocks'), '.0f')} / {_fmt(m.get('p90_duration_blocks'), '.0f')}"),
        ("Median / p90 duration (seconds)", f"{_fmt(m.get('median_duration_s'), '.0f')} / {_fmt(m.get('p90_duration_s'), '.0f')}"),
        ("Episodes touching window edge (censored)", f"{m.get('censored_episodes', 0)}"),
        ("Total gross profit, exact multi-tick (USD)", f"${m['total_gross_usdc']:,.4f}"),
        ("Total gross profit, single-tick approximation (USD), for comparison", f"${m.get('total_gross_usdc_single_tick', 0):,.4f}"),
        ("Total net profit, net-positive episodes only (USD)", f"${m['total_net_usdc']:,.4f}"),
        ("Single-tick peak trade stays before the next tickSpacing boundary (single-tick math provably exact; "
         "crossing a boundary does not mean L changes)", f"{_fmt(m.get('pct_within_tick_range'), '.1f')}% of episodes"),
    ]
    lines = [f"# Replay {run_id}", "", f"**Headline.** {headline}", "", "| Metric | Value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in table]

    if not episodes.is_empty():
        per = (episodes.group_by("pair").agg(
            pl.len().alias("episodes"),
            (pl.col("peak_net_usdc") > 0).sum().alias("net_positive"),
            pl.col("threshold_bps").first(),
            pl.col("peak_gap_bps").max().alias("max_gap_bps"),
            pl.col("duration_blocks").median().alias("median_blocks"),
            pl.col("peak_gross_usdc").sum().alias("gross_usd"),
            pl.col("peak_gross_usdc_single_tick").sum().alias("gross_st_usd"),
            pl.col("peak_gas_usdc").mean().alias("mean_gas_usd"),
        ).sort("pair"))
        lines += ["", "## Per pair", "",
                  "| Pair (fee tiers) | Threshold (bps) | Episodes | Net-positive | Max gap (bps) | Median duration (blocks) | Gross, multi-tick (USD) | Gross, single-tick (USD) | Mean gas (USD) |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for r in per.iter_rows(named=True):
            lines.append(f"| {r['pair']} | {r['threshold_bps']:g} | {r['episodes']} | {r['net_positive']} | {r['max_gap_bps']:.1f} | "
                         f"{r['median_blocks']:.0f} | ${r['gross_usd']:,.4f} | ${r['gross_st_usd']:,.4f} | ${r['mean_gas_usd']:.4f} |")
        lines += single_vs_multi_section(episodes)

    lines += ["", "## Definitions", "",
              "- An **episode** is a run of consecutive blocks in which the same pool pair's end-of-block gap exceeds the "
              "sum of the two pool fees. Duration in blocks = end - start + 1; in seconds = timestamp of the block that "
              "closed it minus the start block's timestamp (Arbitrum timestamps have 1 s resolution, so 1-block episodes "
              "often show 0 s). Durations exclude episodes still open at the window end.",
              "- **Gross** is taken at the episode's peak block; **net** = gross - gas at that block.",
              "- Totals sum one capture per episode.",
              "", "## Gas assumption", "", f"- Method: **{gas['method']}**. {gas['label']}."]
    for pair, g in gas["per_pair"].items():
        lines.append(f"- {pair}: {g['gas_units']:,} gas units (L1 component: {g.get('gas_units_l1')})")
    lines += ["", "## Simplifications (read before using these numbers)", ""] + [f"- {s}" for s in SIMPLIFICATIONS]
    lines += ["", "## Reproducibility", "",
              f"- Git commit `{config['git_commit']}`{' (with uncommitted changes)' if config['git_dirty'] else ''}; "
              f"RPC provider `{config['rpc_provider']}`; registry verified at block {config['registry_verified_at_block']}.",
              "- Full config in `config.json`; episodes in `episodes.parquet`.", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
