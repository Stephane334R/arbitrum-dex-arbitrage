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
from data.collectors.liquidity_events import CACHE_ROOT as LIQ_CACHE_ROOT
from data.collectors.liquidity_events import load_liquidity_events
from data.collectors.tx_details import get_tx_details, get_tx_swaps
from data.collectors.tx_index import get_tx_indices
from data.collectors.pool_state import get_pool_state
from data.collectors.swaps import covered_ranges, load_swaps, missing_ranges, pool_cache_dir
from data.pools.registry import load_registry
from data.pricing.v3_price import usdc_per_weth
from data.rpc import RpcClient, get_web3, load_rpc_url, provider_name, redact
from research.analytics import charts, closers
from research.replay.gas import episode_gas, executor_gas, sensitivity
from research.replay.multitick import size_episodes
from research.replay import txlevel
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
    client, w3 = RpcClient(url, max_retries=cfg.get("rpc_max_retries", 20)), get_web3(url)

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

    pairs = list(combinations(metas, 2))
    exec_gas = executor_gas(cfg["gas"]["forktest_run"])
    gas = {
        "method": "executor_forge_plus_nodeinterface_per_block",
        "label": (f"execute() L2 gas {exec_gas['execute_gas_l2']:,} from the V3ArbExecutor Foundry gas report "
                  f"({exec_gas['source']}; SYNTHETIC trade: {exec_gas['scenario']}) + intrinsic and L1 data gas "
                  "measured per episode with NodeInterface.gasEstimateComponents at the peak block on the episode's "
                  "own execute() calldata; priced at the peak block's base fee"),
        "execute": exec_gas,
    }
    print(f"gas: {gas['label']}")
    if not episodes.is_empty():
        episodes = add_gas(client, episodes, metas, exec_gas, cfg)

    metrics = headline_metrics(segments, episodes, n_swaps, window_ts, (lo, hi))

    tx_cfg = cfg.get("transaction_level") or {}
    tx = None
    if tx_cfg.get("enabled"):
        tx = run_transaction_level(client, w3, cfg, registry, metas, swaps, initial, (lo, hi), window_ts, episodes,
                                   exec_gas)

    # ---- save
    git = git_info()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + (git["commit"][:7] or "nogit")
    out = RESULTS_ROOT / run_id
    out.mkdir(parents=True)
    episodes.write_parquet(out / "episodes.parquet")
    if tx is not None:
        tx["episodes"].write_parquet(out / "tx_episodes.parquet")
        tx["quality"].write_parquet(out / "data_quality.parquet")
        tx["closers"].write_parquet(out / "closers.parquet")
        if tx["closer_summary"]["n_closed"]:
            charts.closers_bar(tx["closer_summary"]["by_to"], tx["closer_summary"]["n_closed"],
                               out / "closers_top_contracts.png", key="to")
    config = {
        "run_id": run_id,
        "block_range": [lo, hi],
        "block_timestamps": list(window_ts),
        "chain_id": registry["chain_id"],
        "registry_verified_at_block": registry["verified_at_block"],
        "pools": [{"address": m.address, "label": m.name, "fee": m.fee, "tick_spacing": m.tick_spacing,
                   "dex": next(p["dex"] for p in registry["pools"] if p["address"] == m.address)} for m in metas],
        "thresholds_bps": {f"{a.name}-{b.name}": {"sum_of_fees": (a.fee + b.fee) / 100,
                                                  "exact_breakeven": exact_breakeven_bps(a.fee, b.fee)}
                           for a, b in pairs},
        "gas_assumption": gas,
        "gas_sensitivity": {"block_level": sensitivity(episodes),
                            **({"transaction_level": sensitivity(tx["episodes"])} if tx is not None else {})},
        "usdc_usd": cfg["usdc_usd"],
        "rpc_provider": provider_name(url),
        "git_commit": git["commit"],
        "git_dirty": git["dirty"],
        "config": cfg,
        "simplifications": SIMPLIFICATIONS,
    }
    if tx is not None:
        config["transaction_level"] = {
            "liquidity_event_cache": "data/cache/liquidity_events (Mint/Burn)",
            "n_events": tx["counts"],
            "snapshot_every_blocks": tx_cfg["snapshot_every_blocks"],
            "checkpoint_blocks": tx["checkpoints"],
            "data_quality": tx["quality_summary"],
            "comparison": tx["comparison"],
            "closers": {k: v for k, v in tx["closer_summary"].items() if not isinstance(v, pl.DataFrame)},
            "simplifications": TX_SIMPLIFICATIONS,
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
                                               return_dtype=pl.Float64).alias(f"p_{m.name}") for i, m in enumerate(metas)])
        charts.episode_prices(price_rows, largest, [m.name for m in metas], out / "largest_episode_prices.png")

    summary = render_summary(run_id, metrics, episodes, gas, config, (lo, hi))
    summary += render_sensitivity(episodes, tx["episodes"] if tx is not None else None)
    if tx is not None:
        summary += render_tx_section(tx, episodes)
    (out / "summary.md").write_text(summary)
    print(f"saved {out.relative_to(ROOT)}")
    print((out / "summary.md").read_text())
    return 0


def add_gas(client: RpcClient, episodes: pl.DataFrame, metas, exec_gas: dict, cfg: dict) -> pl.DataFrame:
    """Per-episode gas at the peak block (research/replay/gas.py) and net = gross - gas. Also
    `executable_by_executor`: V3ArbExecutor verifies both pools against ONE factory (Uniswap v3),
    so pairs with a SushiSwap pool are measured but could not be executed by it as deployed."""
    base = {r["block_number"]: r["base_fee_per_gas"]
            for r in get_headers(client, episodes["peak_block"].unique().to_list()).iter_rows(named=True)}
    uni = {m.address for m in metas if not m.label or m.label.isdigit()}
    out = episode_gas(client, episodes, metas, exec_gas, cfg["usdc_usd"], base)
    return out.with_columns(
        (pl.col("cheap_pool").is_in(list(uni)) & pl.col("expensive_pool").is_in(list(uni))).alias("executable_by_executor"))


def run_transaction_level(client, w3, cfg, registry, metas, swaps, initial, window, window_ts, block_eps,
                          exec_gas) -> dict:
    """Transaction-level replay (research/replay/txlevel.py): events, state after every
    transaction, data-quality checkpoints, episodes, exact sizing, gas, block-level comparison."""
    lo, hi = window
    tx_cfg = cfg["transaction_level"]
    pools = [m.address for m in metas]
    for p in pools:
        gaps = missing_ranges((lo, hi), covered_ranges(pool_cache_dir(p, LIQ_CACHE_ROOT)))
        if gaps:
            raise RuntimeError(f"pool {p} has no cached Mint/Burn logs for {gaps[:3]}; "
                               "run `python -m data.collectors.liquidity_events` first")
    liq = [load_liquidity_events(p, window) for p in pools]
    swap_list = [swaps[p] for p in pools]
    # transactionIndex: from the swap log when the cache file has it (files written since
    # 2026-11-09), else looked up once by tx hash (data/collectors/tx_index.py).
    tx_index = {h.lower(): i for df in swap_list
                for h, i in zip(df["tx_hash"], df["transaction_index"]) if i is not None}
    lookup = sorted({h.lower() for df in swap_list for h in df["tx_hash"]} - set(tx_index))
    if lookup:
        idx = get_tx_indices(client, lookup)
        tx_block = dict(zip(idx["tx_hash"], idx["block_number"]))
        for df in swap_list:  # the looked-up block must be the swap's block
            for h, b in zip(df["tx_hash"], df["block_number"]):
                if h.lower() in tx_block and tx_block[h.lower()] != b:
                    raise RuntimeError(f"tx {h}: looked-up block {tx_block[h.lower()]} != swap block {b}")
        tx_index |= dict(zip(idx["tx_hash"], idx["transaction_index"]))
    events = txlevel.ordered_events(swap_list, liq, tx_index, window)
    init = [initial[p] for p in pools]
    states = txlevel.replay_transactions(events, init, window, window_ts[0])
    counts = {k: int((events["kind"] == k).sum()) for k in ("swap", "mint", "burn")} | {
        "transactions": states.height - 1, "swap_tx_index_looked_up": len(lookup)}
    print(f"transaction level: {counts}")

    checkpoints = txlevel.checkpoint_blocks(window, tx_cfg["snapshot_every_blocks"])
    quality = txlevel.quality_checks(states, swap_list, init, pools, checkpoints,
                                     lambda p, b: get_pool_state(w3, p, b))
    n = quality.height
    qs = {
        "checks": n,
        "mismatch_rate": float((~quality["match"]).mean()),
        "sqrt_mismatches": int((~quality["sqrt_match"]).sum()),
        "tick_mismatches": int((~quality["tick_match"]).sum()),
        "liquidity_mismatches": int((~quality["liq_match"]).sum()),
        "swap_only_liquidity_mismatches": int((~quality["swap_only_liq_match"]).sum()),
    }
    print(f"data quality: {qs}")

    rows = txlevel.tx_pair_rows(states, metas, registry)
    eps = txlevel.find_tx_episodes(rows, window, window_end_close_ts=window_ts[1])
    if not eps.is_empty():
        mt = pl.DataFrame(txlevel.size_tx_episodes(client, eps, states, events, metas, cfg["multitick"]))
        eps = (pl.concat([eps, mt], how="horizontal")
               .rename({"peak_gross_usdc": "peak_gross_usdc_single_tick", "size_usdc": "size_usdc_single_tick"})
               .with_columns(pl.col("mt_gross_usdc").alias("peak_gross_usdc"), pl.col("mt_size_usdc").alias("size_usdc")))
        eps = add_gas(client, eps, metas, exec_gas, cfg)
    eps, cmp = txlevel.compare_levels(eps, block_eps, window)
    print(f"comparison: {cmp}")
    if not cmp["consistent"]:
        raise RuntimeError("transaction-level end-of-block episodes disagree with the block-level replay")

    # Who closed the gaps (research/analytics/closers.py).
    close_rows, close_summary = pl.DataFrame(), {"n_closed": 0}
    if not eps.is_empty():
        hashes = eps.filter(~pl.col("right_censored"))["close_tx_hash"].to_list()
        close_rows = closers.closer_rows(eps, get_tx_details(client, hashes), get_tx_swaps(client, hashes))
        close_summary = closers.summarize(close_rows)
        print(f"closers: {close_summary['n_closed']} closed episodes, {close_summary.get('n_senders')} senders, "
              f"top-5 share {close_summary.get('top5_share_sender')}")
    return {"closers": close_rows, "closer_summary": close_summary,
            "events": events, "states": states, "counts": counts, "checkpoints": checkpoints, "quality": quality,
            "every": tx_cfg["snapshot_every_blocks"],
            "quality_summary": qs, "episodes": eps, "comparison": cmp}


def render_closer_section(cs: dict) -> list[str]:
    """Who closed the gaps: rankings, concentration, delays, and what it says about competition."""
    if not cs.get("n_closed"):
        return ["", "### Who closed the gaps", "", "No closed transaction-level episodes."]
    n = cs["n_closed"]
    f = lambda v, spec="g": "n/a" if v is None else format(v, spec)  # "g": a median of 3.5 stays 3.5
    lines = [
        "", "### Who closed the gaps", "",
        f"For each of the {n} closed transaction-level episodes, the closing transaction is the first transaction on "
        "the pair's pools after which the gap was back under the fee threshold (`closers.parquet`). That is a "
        "definition, not an intent: it can be an arbitrage or an ordinary swap that pushed the price back. "
        "\"Traded both pools\" (events in both pools of the pair in the same transaction) is the arbitrage signature.",
        "",
        "| Metric | Value |", "|---|---|",
        f"| Distinct senders / distinct `to` addresses | {cs['n_senders']} / {cs['n_to']} |",
        f"| Share of gaps closed by the top sender / top 5 senders | {cs['top1_share_sender']:.1%} / "
        f"**{cs['top5_share_sender']:.1%}** |",
        f"| Share closed by the top 5 `to` contracts | {cs['top5_share_to']:.1%} |",
        f"| Closers that traded both pools of the pair | {cs['both_pools_share']:.1%} |",
        f"| Median delay, transactions on the pair's pools (1 = next one) | {f(cs['median_delay_txs'])} |",
        f"| Median delay, blocks (0 = same block) | {f(cs['median_delay_blocks'])} |",
        f"| Median delay, transaction indices (same-block episodes only) | {f(cs['median_tx_index_distance'])} |",
        f"| Median delay (txs / blocks): closers trading both pools vs the others | "
        f"{f(cs['median_delay_txs_both_pools'])} / {f(cs['median_delay_blocks_both_pools'])} vs "
        f"{f(cs['median_delay_txs_other'])} / {f(cs['median_delay_blocks_other'])} |",
        f"| Median closing transaction index in its block / median gas used | {f(cs['median_close_tx_index'])} / "
        f"{f(cs['median_gas_used'], ',.0f')} |",
        f"| Closing transactions that reverted | {cs['failed_closers']} |",
        "",
    ]
    kinds = {k["kind"]: k for k in cs.get("by_kind", [])}
    if kinds:
        labels = {"both_pools": "Both pools of the pair (two-pool arbitrage of this gap)",
                  "multi_pool": "One pool of the pair + other pools (cross-venue arbitrage or multi-hop route)",
                  "single_pool": "Only one pool of the pair (directional trade or CEX-DEX arbitrage)",
                  "unrecognised": "No recognised Swap event"}
        lines += ["Closer kind, from the closing transaction's receipt (recognised Swap events: Uniswap v2/v3/v4 "
                  "shapes, PancakeSwap v3, Balancer v2; not exhaustive, so multi-pool is a lower bound). Each kind "
                  "mixes two intents that the receipt cannot separate; both are named.", "",
                  "| Closer kind | Gaps closed | Share | Same block | Median delay (txs / blocks) | Median close index | "
                  "Median gas used | Gross of those gaps (USD) |", "|---|---|---|---|---|---|---|---|"]
        for k, r in kinds.items():
            if r["n"]:
                lines.append(f"| {labels[k]} | {r['n']} | {r['share']:.1%} | {r['same_block_share']:.0%} | "
                             f"{f(r['median_delay_txs'])} / {f(r['median_delay_blocks'])} | "
                             f"{f(r['median_close_tx_index'])} | {f(r['median_gas_used'], ',.0f')} | "
                             f"${r['gross_usdc']:,.4f} |")
        lines.append("")
    lines += ["Top `to` contracts (searchers often rotate many EOAs behind one contract):", "",
              "| `to` contract | Gaps closed | Share | Senders | Multi-pool or both pools | Median delay (txs / blocks) | "
              "Median gas used | Gross of those gaps (USD) |", "|---|---|---|---|---|---|---|---|"]
    for r in cs["by_to"].iter_rows(named=True):
        mp = r.get("multi_pool_share", 0.0) + r["both_pools_share"]
        lines.append(f"| `{r['to']}` | {r['gaps_closed']} | {r['share']:.1%} | {r['senders']} | {mp:.0%} | "
                     f"{f(r['median_delay_txs'])} / {f(r['median_delay_blocks'])} | "
                     f"{f(r['median_gas_used'], ',.0f')} | ${r['gross_usdc_of_gaps']:,.4f} |")
    lines += ["", "![Top closing contracts](closers_top_contracts.png)", "",
              "Top senders:", "",
              "| Sender | Gaps closed | Share | Most common `to` | Multi-pool or both pools | Median delay (txs / blocks) | "
              "Median gas used | Gross of those gaps (USD) |", "|---|---|---|---|---|---|---|---|"]
    for r in cs["by_sender"].iter_rows(named=True):
        mp = r.get("multi_pool_share", 0.0) + r["both_pools_share"]
        lines.append(f"| `{r['sender']}` | {r['gaps_closed']} | {r['share']:.1%} | `{closers.short(r['top_to'])}` | "
                     f"{mp:.0%} | {f(r['median_delay_txs'])} / {f(r['median_delay_blocks'])} | "
                     f"{f(r['median_gas_used'], ',.0f')} | ${r['gross_usdc_of_gaps']:,.4f} |")
    lines += ["", *render_competition(cs), ""]
    return lines


def render_competition(cs: dict) -> list[str]:
    """What the closer statistics say about competition. Every number is from this run; the
    reading of each number is stated as a reading, not a fact about intent."""
    kinds = {k["kind"]: k for k in cs.get("by_kind", [])}
    share = lambda k: kinds[k]["share"] if k in kinds else 0.0
    n_both = kinds["both_pools"]["n"] if "both_pools" in kinds else round(cs["both_pools_share"] * cs["n_closed"])
    return [
        "#### What this says about competition", "",
        f"- **Speed.** {cs['next_tx_share']:.0%} of gaps were closed by the very next transaction on the pair's pools "
        f"and {cs['same_block_share']:.0%} inside the block where they opened (median closing index "
        f"{cs['median_close_tx_index']:.0f}). A gap is not something to find at a block boundary: to take one, a "
        "transaction must be ordered right behind the one that opened it, which on Arbitrum means winning the "
        "first-come-first-served race to the sequencer (or the Timeboost express lane).",
        f"- **Concentration is in contracts, not in senders.** The top 5 senders closed {cs['top5_share_sender']:.0%} "
        f"of the gaps, but the top 5 `to` contracts closed {cs['top5_share_to']:.0%} ({cs['n_senders']} senders "
        f"behind {cs['n_to']} contracts). A sender ranking alone would make the market look open; a handful of "
        "contracts, each fed by several EOAs, does most of the closing.",
        f"- **{'Almost nobody arbitrages exactly these pairs' if share('both_pools') < 0.1 else 'Two-pool arbitrage of these pairs'}.** "
        f"{'Only ' if share('both_pools') < 0.1 else ''}{n_both} closer{'' if n_both == 1 else 's'} ({share('both_pools'):.1%}) "
        "traded both pools of the pair. "
        f"{share('multi_pool'):.0%} swapped in one of our pools and in other pools (Uniswap v4, v2, other forks): "
        "cross-venue arbitrage or aggregator routes. "
        f"{share('single_pool'):.0%} touched only one of our pools: a directional trade, or a CEX-DEX arbitrage "
        "whose other leg is off-chain."
        + (" Either way, the price that matters is set elsewhere, and the gaps measured here are mostly closed as "
           "a side effect of other trades rather than contested by two-pool arbitrageurs." if share('both_pools') < 0.1
           else ""),
        "- **For this project's executor (a reading, not a measurement).** The gaps that exist are tiny and are closed within about one "
        "transaction by bots trading against a wider set of venues. A two-pool v3 arbitrage contract that waits for "
        "end-of-block state would arrive after almost all of them; competing would need a latency-optimised "
        "backrun and more venues, not a better sizing formula.",
    ]


TX_SIMPLIFICATIONS = [
    "Transaction-level state is rebuilt from Swap (exact post-swap sqrtPriceX96, tick, liquidity) and Mint/Burn "
    "(liquidity +/- amount when tickLower <= tick < tickUpper). Checked against archive slot0()/liquidity() at the "
    "checkpoint blocks listed in config.json, not at every transaction.",
    "Transaction indices come from the logs (transactionIndex) where the cached swap file has them; swaps cached "
    "before that column existed were looked up once by tx hash (eth_getTransactionByHash) and cached.",
    "Exact multi-tick gross at the peak transaction uses the tick table at the end of the previous block with the "
    "replayed intra-block price/tick/liquidity; exact unless a Mint/Burn of that pool earlier in the same block "
    "changed an initialized tick (mt_tick_table_exact).",
    "An intra-block opportunity can only be taken by a transaction ordered right after the opener in the same "
    "block. The gross figures assume that slot was available to us: they measure what existed, not what was "
    "capturable.",
    "duration_txs counts transactions touching the pair's pools only; tx_index_distance (same-block episodes) "
    "counts every transaction in the block between opener and closer.",
]


def _q(s: pl.Series, p: float):
    s = s.drop_nulls()
    return float(s.quantile(p, "nearest")) if s.len() else None


def render_tx_section(tx: dict, block_eps: pl.DataFrame) -> str:
    e, c, qs, n = tx["episodes"], tx["comparison"], tx["quality_summary"], tx["counts"]
    pct = lambda a, b: f"{100 * a / b:.1f}%" if b else "n/a"
    lines = ["", "## Transaction-level replay", "",
             f"State rebuilt after each of {n['transactions']:,} transactions touching the pools "
             f"({n['swap']:,} Swap, {n['mint']:,} Mint, {n['burn']:,} Burn events), ordered by (block, transaction "
             "index, log index).", "",
             "### Data quality", "",
             f"Replayed end-of-block state vs archive `slot0()`/`liquidity()` at {qs['checks']} (block, pool) "
             f"checkpoints: every {tx['every']:,} blocks plus the window's last block, {len(tx['checkpoints'])} blocks "
             "(listed in config.json), checked for every pool.", "",
             "| Check | Mismatches |", "|---|---|",
             f"| Any field (sqrtPriceX96, tick or liquidity): **mismatch rate** | {qs['checks'] - int(tx['quality']['match'].sum())} "
             f"/ {qs['checks']} ({100 * qs['mismatch_rate']:.2f}%) |",
             f"| sqrtPriceX96 | {qs['sqrt_mismatches']} |",
             f"| tick | {qs['tick_mismatches']} |",
             f"| liquidity, transaction-level (Swap + Mint/Burn) | {qs['liquidity_mismatches']} |",
             f"| liquidity, swap-only (what the block-level replay uses) | {qs['swap_only_liquidity_mismatches']} |",
             ""]
    if e.is_empty():
        return "\n".join(lines + ["No transaction-level episodes.", ""])

    closed = e.filter(~pl.col("right_censored"))
    missed = e.filter(pl.col("block_episode_idx").is_null())
    vis = e.filter(pl.col("block_episode_idx").is_not_null())
    gsum = lambda d, col="peak_gross_usdc": float(d[col].sum()) if d.height else 0.0
    npos = lambda d: int((d["peak_net_usdc"] > 0).sum()) if d.height else 0
    b_gross = float(block_eps["peak_gross_usdc"].sum()) if not block_eps.is_empty() else 0.0
    b_pos = int((block_eps["peak_net_usdc"] > 0).sum()) if not block_eps.is_empty() else 0
    fmt = lambda v, spec=".0f": "n/a" if v is None else format(v, spec)
    lines += [
        "### Block-level vs transaction-level", "",
        "| Metric | Block-level | Transaction-level |", "|---|---|---|",
        f"| Episodes | {c['n_block']} | {c['n_tx']} |",
        f"| Net-positive episodes | {b_pos} | {npos(e)} |",
        f"| Gross profit, exact multi-tick, one capture per episode (USD) | ${b_gross:,.4f} | ${gsum(e):,.4f} |",
        f"| Opened and closed inside one block | (invisible) | {int(e['intra_block'].sum())} "
        f"({pct(int(e['intra_block'].sum()), closed.height)} of closed) |",
        f"| Median / p90 duration, transactions on the pair's pools | n/a | {fmt(_q(closed['duration_txs'], .5))} / "
        f"{fmt(_q(closed['duration_txs'], .9))} |",
        f"| Median / p90 duration, blocks (transaction level: close - start block, 0 = same block) | "
        f"{fmt(_q(block_eps['duration_blocks'], .5)) if not block_eps.is_empty() else 'n/a'} / "
        f"{fmt(_q(block_eps['duration_blocks'], .9)) if not block_eps.is_empty() else 'n/a'} | "
        f"{fmt(_q(closed['duration_blocks'], .5))} / {fmt(_q(closed['duration_blocks'], .9))} |",
        f"| Closing transaction trades both pools of the pair | n/a | "
        f"{pct(int(closed['closer_touches_both_pools'].sum()), closed.height)} of closed |",
        "",
        f"**Missed by the block-level replay: {c['n_missed']} of {c['n_tx']} transaction-level episodes "
        f"({pct(c['n_missed'], c['n_tx'])})**, with exact gross ${gsum(missed):,.4f} "
        f"({pct(gsum(missed), gsum(e))} of the transaction-level total) and {npos(missed)} net-positive.",
    ]
    if missed.height:
        lines += [
            f"Their peak gaps: median {fmt(_q(missed['peak_gap_bps'], .5), '.2f')} bps, max "
            f"{fmt(missed['peak_gap_bps'].max(), '.2f')} bps; median gross ${fmt(_q(missed['peak_gross_usdc'], .5), '.6f')}, "
            f"max ${fmt(missed['peak_gross_usdc'].max(), '.6f')}. Median duration {fmt(_q(missed['duration_txs'], .5))} "
            f"transaction(s) on the pair's pools, median {fmt(_q(missed['tx_index_distance'], .5))} transaction "
            "indices between opener and closer.",
        ]
    lines += [
        "",
        f"The {vis.height} visible transaction-level episodes map onto {c['n_block_matched']} block-level episodes; "
        f"{c['n_block_with_several_tx_episodes']} block-level episode(s) contain more than one transaction-level "
        "episode (the gap closed and reopened inside a block). Consistency check: the blocks at whose end a "
        "transaction-level episode is open equal the block-level episode blocks "
        f"(**{'passed' if c['consistent'] else 'FAILED'}**).",
        f"Exact multi-tick tick table exact for {int(e['mt_tick_table_exact'].sum())} of {e.height} episodes "
        "(see simplifications).",
        "",
        "| Pair | Start (block / tx idx) | Close (block / tx idx) | Txs | Gap (bps) | Gross, exact (USD) | Net (USD) | "
        "Block-level | Closer trades both pools |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    shown = e.sort("peak_gross_usdc", descending=True).head(40).sort("start_seq")
    for r in shown.iter_rows(named=True):
        close = "open at window end" if r["right_censored"] else f"{r['close_block']} / {r['close_tx_index']}"
        seen = "missed" if r["block_episode_idx"] is None else f"episode #{r['block_episode_idx']}"
        lines.append(
            f"| {r['pair']} | {r['start_block']} / {r['start_tx_index']} | {close} | {r['duration_txs']} | "
            f"{r['peak_gap_bps']:.2f} | {r['peak_gross_usdc']:.6f} | {r['peak_net_usdc']:.6f} | {seen} | "
            f"{'yes' if r['closer_touches_both_pools'] else 'no'} |")
    if e.height > shown.height:
        lines.append(f"\n(Top {shown.height} of {e.height} episodes by gross; all in `tx_episodes.parquet`.)")
    lines += render_closer_section(tx["closer_summary"])
    lines += ["", "### Transaction-level simplifications", ""] + [f"- {s}" for s in TX_SIMPLIFICATIONS] + [""]
    return "\n".join(lines)


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
    "Block-level states are end-of-block; gaps opened and closed within one block are invisible to the block-level "
    "replay (the transaction-level section, when enabled, measures them).",
    "Pairs are evaluated independently; simultaneous episodes on pairs sharing a pool compete for the same "
    "liquidity, so summed profits overstate what one arbitrageur could capture.",
    "Gas = execute() L2 gas from ONE SYNTHETIC fork-test trade (50,000 USDC, 0.30% -> 0.05%, many ticks crossed: "
    "probably an overestimate for small arbitrages) + intrinsic and L1 data gas measured per episode at its peak block "
    "(NodeInterface, archive eth_call). See the gas sensitivity table for x0.5 / x2.",
    "V3ArbExecutor verifies pools against the Uniswap v3 factory only: episodes on pairs with the SushiSwap pool are "
    "real price gaps but not executable by the contract as deployed (executable_by_executor = false).",
    "Profits in USDC with 1 USDC = 1 USD.",
]


def _range(col: pl.Series, spec: str = ",.0f") -> str:
    lo, hi = col.min(), col.max()
    return format(lo, spec) if lo == hi else f"{format(lo, spec)} - {format(hi, spec)}"


def gas_lines(episodes: pl.DataFrame, gas: dict) -> list[str]:
    e = gas["execute"]
    lines = [f"- execute() L2 gas: **{e['execute_gas_l2']:,}** (forge, fork block {e['fork_block']}, commit "
             f"`{e['git_commit'][:7]}`). For reference, all {e['all_execute_calls']['calls']} execute() calls in the "
             f"fork suite (reverts and fuzz sizes included): median {e['all_execute_calls']['median']:,}."]
    if episodes.is_empty():
        return lines
    lines += [
        f"- Intrinsic L2 gas (NodeInterface, per peak block): {_range(episodes['gas_units_intrinsic_l2'])}.",
        f"- L1 data gas (NodeInterface gasEstimateForL1, per peak block): {_range(episodes['gas_units_l1'])}; "
        f"L1 base fee estimate {_range(episodes['l1_base_fee_estimate_wei'])} wei "
        f"(ArbGasInfo: {_range(episodes['agi_l1_base_fee_estimate_wei'])} wei, "
        f"{_range(episodes['wei_per_l1_calldata_byte'])} wei per L1 calldata byte) over "
        f"{episodes['peak_block'].n_unique()} peak blocks.",
        f"- Total gas units per episode: {_range(episodes['gas_units'])}; base fee at peak blocks "
        f"{_range(episodes['base_fee_wei'] / 1e9, ',.4f')} gwei (header; NodeInterface reports "
        f"{_range(episodes['ni_base_fee_wei'] / 1e9, ',.4f')} gwei); cost {_range(episodes['peak_gas_usdc'], ',.4f')} USD.",
    ]
    return lines


def render_sensitivity(block_eps: pl.DataFrame, tx_eps: pl.DataFrame | None) -> str:
    """Net-positive episodes M (and their total net) when gas is scaled by 0.5, 1 and 2."""
    b = sensitivity(block_eps)
    t = sensitivity(tx_eps) if tx_eps is not None else None
    lines = ["", "## Gas sensitivity", "",
             "M = episodes with exact gross > k x gas (gas = forge execute() + measured intrinsic and L1 gas, at the "
             "peak block's base fee).", ""]
    head = "| Gas multiplier k | M, block-level | Net, block-level (USD) |"
    sep = "|---|---|---|"
    if t is not None:
        head += " M, transaction-level | Net, transaction-level (USD) |"
        sep += "---|---|"
    lines += [head, sep]
    for i, r in enumerate(b):
        row = f"| x{r['k']:g} | {r['M']} | ${r['net_usdc']:,.4f} |"
        if t is not None:
            row += f" {t[i]['M']} | ${t[i]['net_usdc']:,.4f} |"
        lines.append(row)
    if not block_eps.is_empty() and "executable_by_executor" in block_eps.columns:
        ex = block_eps.filter(pl.col("executable_by_executor"))
        lines += ["", f"Of the {block_eps.height} block-level episodes, {ex.height} are on Uniswap-only pairs "
                      f"(executable by V3ArbExecutor as deployed); M at x1 among them: {sensitivity(ex)[1]['M']}."]
    return "\n".join(lines + [""])


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
    top = e.sort("peak_gross_usdc", descending=True).head(40).sort("peak_block")
    for r in top.iter_rows(named=True):
        lines.append(
            f"| {r['pair']} | {r['peak_block']} | {r['peak_gap_bps']:.2f} | {r['size_usdc_single_tick']:,.2f} / "
            f"{r['peak_gross_usdc_single_tick']:.6f} | {r['st_onchain_l_gross_usdc']:.6f} | {r['size_usdc']:,.2f} / "
            f"{r['peak_gross_usdc']:.6f} | {r['mt_ticks_crossed_cheap']}, {r['mt_ticks_crossed_expensive']} | "
            f"{'yes' if r['liquidity_matches_replay'] else 'no'} |")
    if e.height > top.height:
        lines += ["", f"(Top {top.height} of {e.height} episodes by exact gross, in block order; all in `episodes.parquet`.)"]
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
    lines += gas_lines(episodes, gas)
    lines += ["", "## Simplifications (read before using these numbers)", ""] + [f"- {s}" for s in SIMPLIFICATIONS]
    lines += ["", "## Reproducibility", "",
              f"- Git commit `{config['git_commit']}`{' (with uncommitted changes)' if config['git_dirty'] else ''}; "
              f"RPC provider `{config['rpc_provider']}`; registry verified at block {config['registry_verified_at_block']}.",
              "- Full config in `config.json`; episodes in `episodes.parquet`.", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
