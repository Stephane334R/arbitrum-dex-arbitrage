"""Static PNG charts for replay results (matplotlib).

Colours: first three slots of the validated categorical palette (blue, orange, aqua), fixed
per pool fee tier so a pool keeps its colour in every chart. Thin marks, hairline solid
grid, text in neutral inks, direct labels in addition to legends.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.ticker  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
GRID = "#e4e3df"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]  # categorical palette slots 1-4, fixed order


def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    ax.grid(True, color=GRID, linewidth=0.8, linestyle="-")
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=TEXT_2, labelsize=9)
    ax.xaxis.label.set_color(TEXT_2)
    ax.yaxis.label.set_color(TEXT_2)


def _fig(nrows=1, ncols=1, **kw):
    fig, axes = plt.subplots(nrows, ncols, facecolor=SURFACE, **kw)
    for ax in np.atleast_1d(axes).ravel():
        _style(ax)
    return fig, axes


def gap_distribution(rows: pl.DataFrame, path: Path) -> None:
    """Per-block gap distribution per pair (small multiples), fee threshold marked."""
    pairs = rows["pair"].unique(maintain_order=True).to_list()
    fig, axes = _fig(1, len(pairs), figsize=(4.2 * len(pairs), 3.6), sharey=True)
    axes = np.atleast_1d(axes)
    for ax, pair in zip(axes, pairs):
        d = rows.filter(pl.col("pair") == pair)
        thr = float(d["threshold_bps"][0])
        gaps, w = d["gap_bps"].to_numpy(), d["n_blocks"].to_numpy()
        hi = max(np.quantile(np.repeat(gaps, w), 0.999), thr) * 1.15
        bins = np.linspace(0, hi, 50)
        ax.hist(np.clip(gaps, 0, hi), bins=bins, weights=w, color=SERIES[0], edgecolor=SURFACE, linewidth=0.6)
        ax.axvline(thr, color=TEXT, linewidth=1.2)
        above = 100 * w[gaps > thr].sum() / w.sum()
        ax.text(thr, 0.97, f" fee threshold {thr:g} bps\n {above:.2f}% of blocks above", transform=ax.get_xaxis_transform(),
                va="top", ha="left", fontsize=8.5, color=TEXT)
        ax.set_yscale("log")
        ax.set_title(f"pair {pair} (fee tiers)", fontsize=10, color=TEXT, loc="left")
        ax.set_xlabel("price gap (bps)")
    axes[0].set_ylabel("blocks (log scale)")
    fig.suptitle("Per-block price gap between pools, per pair (gaps beyond the axis are counted in the last bin)", fontsize=11, color=TEXT, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def duration_histogram(episodes: pl.DataFrame, path: Path) -> None:
    fig, ax = _fig(figsize=(6.5, 3.6))
    closed = episodes.filter(~pl.col("right_censored"))
    d = closed["duration_blocks"].to_numpy()
    if len(d):
        edges = np.unique(np.concatenate([[1, 2, 3, 4, 5, 6, 11, 21, 51, 101, 201], [d.max() + 1]]))
        edges = edges[edges <= d.max() + 1]
        counts, _ = np.histogram(d, bins=edges)
        labels = [f"{a}" if b - a == 1 else f"{a}-{b - 1}" for a, b in zip(edges[:-1], edges[1:])]
        x = np.arange(len(counts))
        ax.bar(x, counts, color=SERIES[0], width=0.8)
        for xi, c in zip(x, counts):
            if c:
                ax.text(xi, c, f"{c}", ha="center", va="bottom", fontsize=8.5, color=TEXT)
        ax.set_xticks(x, labels)
    ax.set_xlabel("episode duration (blocks)")
    ax.set_ylabel("episodes")
    n_cens = int(episodes["right_censored"].sum()) if episodes.height else 0
    ax.set_title(f"Episode duration ({len(d)} closed episodes; {n_cens} still open at window end excluded)",
                 fontsize=10, color=TEXT, loc="left")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def pool_display_name(label: str) -> str:
    """'500' -> '0.05% Uni v3', 'sushi500' -> '0.05% Sushi v3' (registry pool labels)."""
    digits = "".join(ch for ch in label if ch.isdigit())
    dex = "Uni v3" if label.isdigit() else label[: len(label) - len(digits)].capitalize() + " v3"
    return f"{int(digits) / 1_000_000:.2%} {dex}"


def episode_prices(price_rows: pl.DataFrame, episode: dict, labels: list[str], path: Path, pad_blocks: int = 40) -> None:
    """Step plot of each pool's price (USDC/WETH) around one episode, episode shaded.
    `price_rows`: block_start plus one price column per pool label, e.g. 'p_500', 'p_sushi500'."""
    lo, hi = episode["start_block"] - pad_blocks, episode["end_block"] + pad_blocks
    lo = max(lo, int(price_rows["block_start"].min()))
    first = price_rows.filter(pl.col("block_start") <= lo)["block_start"].max()  # state in force at `lo`
    d = price_rows.filter(pl.col("block_start").is_between(first, hi))
    fig, ax = _fig(figsize=(8, 4))
    x = d["block_start"].to_numpy()
    for k, label in enumerate(labels):
        y = d[f"p_{label}"].to_numpy()
        xs, ys = np.append(np.maximum(x, lo), hi), np.append(y, y[-1])
        name = pool_display_name(label)
        ax.step(xs, ys, where="post", color=SERIES[k % len(SERIES)], linewidth=2, label=name)
        ax.text(hi, ys[-1], f"  {name}", color=TEXT, fontsize=8.5, va="center")
    ax.axvspan(episode["start_block"], episode["end_block"] + 1, color=TEXT_2, alpha=0.12, linewidth=0)
    ax.text(episode["start_block"], 1.0, f" episode {episode['pair']}: peak gap {episode['peak_gap_bps']:.1f} bps,\n"
            f" gross ${episode['peak_gross_usdc']:.4f} (exact multi-tick)",
            transform=ax.get_xaxis_transform(), va="top", fontsize=8.5, color=TEXT)
    ax.set_xlim(lo, hi + (hi - lo) * 0.08)
    ax.ticklabel_format(axis="x", style="plain", useOffset=False)
    ax.set_xlabel("block number")
    ax.set_ylabel("USDC per WETH (end of block)")
    ax.legend(frameon=False, fontsize=8.5, loc="lower left", labelcolor=TEXT)
    ax.set_title("Pool prices around the largest episode (by peak gross profit)", fontsize=10, color=TEXT, loc="left")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def closers_bar(ranking: pl.DataFrame, n_closed: int, path: Path, key: str = "to") -> None:
    """Top closing-transaction `to` contracts (key="to") or senders (key="sender") by gaps closed:
    horizontal bars, one series, value at the tip. Closer kind is not encoded by colour: identity
    stays text, and the label carries the share and (for contracts) the number of senders and the
    multi-pool share."""
    d = ranking.head(10)
    n = d.height
    fig, ax = _fig(figsize=(7.5, 0.42 * max(n, 3) + 1.3))
    if n:
        y = np.arange(n)[::-1]
        counts = d["gaps_closed"].to_numpy()
        # 0.42 in per row at 150 dpi = 63 px; height 0.35 -> about 22 px bars (spec: <= 24 px).
        ax.barh(y, counts, height=0.35, color=SERIES[0])
        ax.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
        ax.set_yticks(y, [f"{s[:6]}…{s[-4:]}" for s in d[key].to_list()], fontsize=8.5, family="monospace")
        for i, (yi, c) in enumerate(zip(y, counts)):
            r = d.row(i, named=True)
            parts = [f"{c} ({r['share']:.0%})"]
            if key == "to":
                parts.append(f"{r['senders']} sender{'s' if r['senders'] != 1 else ''}")
            if "multi_pool_share" in r:
                parts.append(f"{r['multi_pool_share'] + r['both_pools_share']:.0%} multi-pool")
            else:
                parts.append(f"{r['both_pools_share']:.0%} traded both pools")
            ax.text(c, yi, "  " + " · ".join(parts), va="center", fontsize=8.5, color=TEXT)
        ax.set_xlim(0, counts.max() * 2.0)
        ax.grid(axis="y", visible=False)
    ax.set_xlabel("gaps closed (transaction-level episodes)")
    what = "contracts (tx 'to')" if key == "to" else "senders"
    ax.set_title(f"Gaps closed by the top {n} {what}, of {n_closed} closed episodes",
                 fontsize=10, color=TEXT, loc="left")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
