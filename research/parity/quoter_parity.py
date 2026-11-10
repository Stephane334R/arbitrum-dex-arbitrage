"""Parity run: data/pricing/v3_swap.py vs Uniswap QuoterV2 on Arbitrum One, at one pinned block.

For each verified pool (configs/pools.yaml), direction and size (configs/v3_parity.yaml):
  1. QuoterV2.quoteExactInputSingle via eth_call at the block (sqrtPriceLimitX96 = 0);
  2. the Python port on a tick snapshot of the same pool at the same block
     (data/collectors/ticks.py);
  3. compare amountOut, sqrtPriceX96After and initializedTicksCrossed for exact equality.

Before quoting, the run verifies from the chain:
  - QuoterV2 has code at the block, and its factory() / WETH9() match the registry's factory and
    WETH (so it quotes the same pools we read);
  - factory.getPool(token0, token1, fee) at the block still returns each registry pool.

Outputs:
  tests/vectors/v3_swaps.json   pool snapshots + vectors (reused by the offline parity test,
                                and meant for a Foundry test later)
  research/results/<run_id>/    config.json + summary.md
QuoterV2 answers are cached in data/cache/quoter/<quoter>/<block>.parquet.

Usage:  uv run python -m research.parity.quoter_parity
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import polars as pl
import yaml
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from data.collectors.ticks import get_tick_snapshot
from data.pools.registry import load_registry
from data.pricing.v3_swap import (
    PoolSnapshot,
    TickDataOutOfRange,
    count_initialized_ticks_crossed,
    quote_exact_input_single,
)
from data.rpc import ARBITRUM_CHAIN_ID, RpcClient, load_rpc_url, provider_name, redact
from research.replay.run import git_info

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "configs" / "v3_parity.yaml"
VECTORS_PATH = ROOT / "tests" / "vectors" / "v3_swaps.json"
RESULTS_ROOT = ROOT / "research" / "results"
QUOTER_CACHE = ROOT / "data" / "cache" / "quoter"

QUOTE_SIG = "quoteExactInputSingle((address,address,uint256,uint24,uint160))"
QUOTE_SCHEMA = {
    "token_in": pl.Utf8,
    "token_out": pl.Utf8,
    "fee": pl.Int32,
    "amount_in": pl.Utf8,
    "amount_out": pl.Utf8,
    "sqrt_price_x96_after": pl.Utf8,
    "initialized_ticks_crossed": pl.Int64,
    "gas_estimate": pl.Utf8,
}


class VerificationError(Exception):
    pass


def _sel(sig: str) -> str:
    return "0x" + keccak(text=sig).hex()[:8]


def _eth_call(rpc: RpcClient, to: str, data: str, block: int, types: list[str]) -> tuple:
    raw = rpc.call("eth_call", [{"to": to, "data": data}, hex(block)])
    return abi_decode(types, bytes.fromhex(raw[2:]))


# --- on-chain verification ----------------------------------------------------------------------


def verify_quoter(rpc: RpcClient, quoter: str, registry: dict, block: int) -> dict:
    quoter = to_checksum_address(quoter)
    code = rpc.call("eth_getCode", [quoter, hex(block)])
    if len(code) <= 2:
        raise VerificationError(f"QuoterV2 {quoter}: no code at block {block}")
    (factory,) = _eth_call(rpc, quoter, _sel("factory()"), block, ["address"])
    (weth9,) = _eth_call(rpc, quoter, _sel("WETH9()"), block, ["address"])
    factory, weth9 = to_checksum_address(factory), to_checksum_address(weth9)
    if factory != registry["factory"]["address"]:
        raise VerificationError(f"QuoterV2 factory() {factory} != registry factory")
    if weth9 != registry["tokens"]["WETH"]["address"]:
        raise VerificationError(f"QuoterV2 WETH9() {weth9} != registry WETH")
    return {"address": quoter, "factory": factory, "weth9": weth9, "code_bytes": (len(code) - 2) // 2}


def uniswap_pools(registry: dict) -> list[dict]:
    """QuoterV2 parity covers the Uniswap v3 pools only (the Sushi pool has its own quoter)."""
    return [p for p in registry["pools"] if p.get("dex", "uniswap_v3") == "uniswap_v3"]


def verify_pools_at_block(rpc: RpcClient, registry: dict, block: int) -> None:
    factory = registry["factory"]["address"]
    for p in uniswap_pools(registry):
        data = _sel("getPool(address,address,uint24)") + abi_encode(
            ["address", "address", "uint24"], [p["token0"]["address"], p["token1"]["address"], p["fee"]]
        ).hex()
        (addr,) = _eth_call(rpc, factory, data, block, ["address"])
        if to_checksum_address(addr) != p["address"]:
            raise VerificationError(f"getPool(fee={p['fee']}) at {block} is {addr}, registry has {p['address']}")


# --- QuoterV2 with a Parquet cache --------------------------------------------------------------


class QuoterCache:
    def __init__(self, quoter: str, block: int, root: Path = QUOTER_CACHE):
        self.path = root / quoter.lower() / f"{block:012d}.parquet"
        self.rows: dict[tuple, dict] = {}
        if self.path.exists():
            for r in pl.read_parquet(self.path).iter_rows(named=True):
                self.rows[(r["token_in"], r["token_out"], r["fee"], r["amount_in"])] = r

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".parquet.tmp")
        pl.DataFrame(list(self.rows.values()), schema=QUOTE_SCHEMA).write_parquet(tmp)
        os.replace(tmp, self.path)


def quote_onchain(rpc, cache: QuoterCache, quoter, token_in, token_out, fee, amount_in, block) -> dict:
    key = (token_in, token_out, fee, str(amount_in))
    if key not in cache.rows:
        data = _sel(QUOTE_SIG) + abi_encode(
            ["(address,address,uint256,uint24,uint160)"], [(token_in, token_out, amount_in, fee, 0)]
        ).hex()
        out, sqrt_after, crossed, gas = _eth_call(rpc, quoter, data, block, ["uint256", "uint160", "uint32", "uint256"])
        cache.rows[key] = {
            "token_in": token_in,
            "token_out": token_out,
            "fee": fee,
            "amount_in": str(amount_in),
            "amount_out": str(out),
            "sqrt_price_x96_after": str(sqrt_after),
            "initialized_ticks_crossed": crossed,
            "gas_estimate": str(gas),
        }
    return cache.rows[key]


# --- snapshot <-> JSON (also used by tests/test_v3_swap_parity.py) ------------------------------


def snapshot_to_json(snap: PoolSnapshot) -> dict:
    return {
        "sqrt_price_x96": str(snap.sqrt_price_x96),
        "tick": snap.tick,
        "liquidity": str(snap.liquidity),
        "fee": snap.fee,
        "tick_spacing": snap.tick_spacing,
        "word_lo": snap.word_lo,
        "word_hi": snap.word_hi,
        "bitmap": {str(w): hex(v) for w, v in sorted(snap.bitmap.items())},
        "ticks": [{"tick": t, "liquidity_net": str(n)} for t, n in sorted(snap.liquidity_net.items())],
    }


def snapshot_from_json(d: dict) -> PoolSnapshot:
    return PoolSnapshot(
        sqrt_price_x96=int(d["sqrt_price_x96"]),
        tick=d["tick"],
        liquidity=int(d["liquidity"]),
        fee=d["fee"],
        tick_spacing=d["tick_spacing"],
        bitmap={int(w): int(v, 16) for w, v in d["bitmap"].items()},
        liquidity_net={t["tick"]: int(t["liquidity_net"]) for t in d["ticks"]},
        word_lo=d["word_lo"],
        word_hi=d["word_hi"],
    )


def python_quote(snap: PoolSnapshot, zero_for_one: bool, amount_in: int) -> dict:
    res = quote_exact_input_single(snap, zero_for_one, amount_in)
    return {
        "amount_in_consumed": str(res.amount_in),
        "amount_out": str(res.amount_out),
        "fee_amount": str(res.fee_amount),
        "sqrt_price_x96_after": str(res.sqrt_price_x96_after),
        "tick_after": res.tick_after,
        "ticks_crossed": res.ticks_crossed,
        "quoter_initialized_ticks_crossed": count_initialized_ticks_crossed(snap, snap.tick, res.tick_after),
        "steps": res.steps,
    }


def compare(quoter: dict, py: dict) -> dict:
    return {
        "amount_out": quoter["amount_out"] == py["amount_out"],
        "sqrt_price_x96_after": quoter["sqrt_price_x96_after"] == py["sqrt_price_x96_after"],
        "initialized_ticks_crossed": quoter["initialized_ticks_crossed"] == py["quoter_initialized_ticks_crossed"],
    }


# --- run ----------------------------------------------------------------------------------------


def to_raw(amount: str, decimals: int) -> int:
    raw = Decimal(amount) * (Decimal(10) ** decimals)
    if raw != raw.to_integral_value() or raw <= 0:
        raise ValueError(f"size {amount} is not a positive whole number of raw units at {decimals} decimals")
    return int(raw)


def run(url: str) -> int:
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    registry = load_registry()
    block = int(cfg["block"])
    rpc = RpcClient(url)

    chain_id = int(rpc.call("eth_chainId", []), 16)
    if chain_id != ARBITRUM_CHAIN_ID:
        raise VerificationError(f"RPC is on chain {chain_id}, expected {ARBITRUM_CHAIN_ID}")
    quoter = verify_quoter(rpc, cfg["quoter_v2"], registry, block)
    verify_pools_at_block(rpc, registry, block)
    print(f"verified QuoterV2 {quoter['address']} and {len(uniswap_pools(registry))} pools at block {block}")

    cache = QuoterCache(quoter["address"], block)
    pools_json, vectors = [], []
    for p in uniswap_pools(registry):
        words = cfg["words_each_side"]
        snap = get_tick_snapshot(rpc, p["address"], block, words)
        if snap.fee != p["fee"] or snap.tick_spacing != p["tick_spacing"]:
            raise VerificationError(f"{p['address']}: fee/tickSpacing at {block} differ from the registry")
        for t_in, t_out in ((p["token0"], p["token1"]), (p["token1"], p["token0"])):
            zero_for_one = t_in["address"] == p["token0"]["address"]
            sizes = cfg["sizes"].get(p["fee"], cfg["sizes"]["default"])
            for size in sizes[t_in["symbol"]]:
                amount_in = to_raw(size, t_in["decimals"])
                q = quote_onchain(rpc, cache, quoter["address"], t_in["address"], t_out["address"], p["fee"], amount_in, block)
                while True:
                    try:
                        py = python_quote(snap, zero_for_one, amount_in)
                        break
                    except TickDataOutOfRange:
                        if words >= cfg["max_words_each_side"]:
                            raise
                        words += cfg["widen_step"]
                        snap = get_tick_snapshot(rpc, p["address"], block, words)
                quoter_out = {k: q[k] for k in ("amount_out", "sqrt_price_x96_after", "initialized_ticks_crossed", "gas_estimate")}
                match = compare(quoter_out, py)
                vectors.append(
                    {
                        "id": f"fee{p['fee']}_{t_in['symbol']}_to_{t_out['symbol']}_{size}",
                        "pool": p["address"],
                        "fee": p["fee"],
                        "token_in": t_in["address"],
                        "token_out": t_out["address"],
                        "token_in_symbol": t_in["symbol"],
                        "token_out_symbol": t_out["symbol"],
                        "zero_for_one": zero_for_one,
                        "amount_in_human": size,
                        "amount_in": str(amount_in),
                        "quoter": quoter_out,
                        "python": py,
                        "match": match,
                    }
                )
                flag = "OK  " if all(match.values()) else "DIFF"
                print(
                    f"  {flag} fee={p['fee']:>4} {t_in['symbol']}->{t_out['symbol']} {size:>10}: "
                    f"out={q['amount_out']} crossed={len(py['ticks_crossed'])} steps={py['steps']}"
                )
        cache.save()
        pools_json.append({"address": p["address"], "token0": p["token0"], "token1": p["token1"], **snapshot_to_json(snap)})

    git = git_info()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + (git["commit"][:7] or "nogit")
    n_ok = sum(all(v["match"].values()) for v in vectors)
    meta = {
        "description": "Uniswap v3 exact-input swaps: Python port (data/pricing/v3_swap.py) vs QuoterV2 eth_call",
        "run_id": run_id,
        "chain_id": ARBITRUM_CHAIN_ID,
        "block": block,
        "state_semantics": "state after `block` (eth_call at that block number)",
        "quoter_v2": quoter,
        "sqrt_price_limit_x96": "0 (QuoterV2 default: MIN_SQRT_RATIO+1 / MAX_SQRT_RATIO-1)",
        "rpc_provider": provider_name(url),
        "git_commit": git["commit"],
        "git_dirty": git["dirty"],
        "number_format": "integers > 2^53 are decimal strings; bitmap words are hex strings",
        "initialized_ticks_crossed_note": (
            "QuoterV2 counts bitmap bits between tickBefore and tickAfter (PoolTicksCounter); "
            "python.quoter_initialized_ticks_crossed ports that, python.ticks_crossed lists the ticks "
            "the swap loop actually crossed"
        ),
        "vectors_matching": f"{n_ok}/{len(vectors)}",
    }
    VECTORS_PATH.parent.mkdir(parents=True, exist_ok=True)
    VECTORS_PATH.write_text(json.dumps({"meta": meta, "pools": pools_json, "vectors": vectors}, indent=2) + "\n")

    out = RESULTS_ROOT / run_id
    out.mkdir(parents=True, exist_ok=True)
    config = {**meta, "config": cfg, "pools": [{k: p[k] for k in ("address", "fee", "tick_spacing")} for p in uniswap_pools(registry)],
              "word_windows": {pj["address"]: [pj["word_lo"], pj["word_hi"]] for pj in pools_json},
              "vectors_file": str(VECTORS_PATH.relative_to(ROOT)), "rpc_calls": rpc.calls}
    (out / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    (out / "summary.md").write_text(render_summary(meta, vectors))
    print(f"{n_ok}/{len(vectors)} vectors match exactly; wrote {VECTORS_PATH.relative_to(ROOT)} and {out.relative_to(ROOT)}")
    return 0 if n_ok == len(vectors) else 1


def render_summary(meta: dict, vectors: list[dict]) -> str:
    lines = [
        f"# QuoterV2 parity {meta['run_id']}",
        "",
        f"**Result.** {meta['vectors_matching']} exact-input quotes match QuoterV2 "
        f"({meta['quoter_v2']['address']}) exactly on amountOut, sqrtPriceX96After and "
        f"initializedTicksCrossed, at Arbitrum One block {meta['block']}.",
        "",
        "| Vector | amountIn (raw) | amountOut (raw) | ticks crossed | loop steps | match |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for v in vectors:
        ok = "yes" if all(v["match"].values()) else "NO: " + ", ".join(k for k, m in v["match"].items() if not m)
        lines.append(
            f"| {v['id']} | {v['amount_in']} | {v['quoter']['amount_out']} | "
            f"{len(v['python']['ticks_crossed'])} | {v['python']['steps']} | {ok} |"
        )
    lines += [
        "",
        "## Reproduce",
        "",
        f"- Config: `configs/v3_parity.yaml`; pools: `configs/pools.yaml`; RPC provider {meta['rpc_provider']}; "
        f"git commit `{meta['git_commit']}`{' (with uncommitted changes)' if meta['git_dirty'] else ''}.",
        "- `uv run python -m research.parity.quoter_parity`, then `uv run pytest tests/test_v3_swap_parity.py`.",
        "",
        "## Scope",
        "",
        "- Exact-input, single-pool quotes only. The exact-output path of the port is unit-tested "
        "(tests/test_v3_swap_math.py) but not checked against QuoterV2 here.",
        "- One block, WETH/USDC pools only. Parity at one block is strong evidence, not proof, of "
        "parity in every state.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    url = load_rpc_url()
    try:
        return run(url)
    except Exception as e:  # never let the RPC URL leak through an error message
        print(f"parity run failed: {redact(e, url)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
