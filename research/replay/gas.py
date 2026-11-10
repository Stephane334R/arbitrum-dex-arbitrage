"""Gas cost of executing an episode's arbitrage with contracts/src/V3ArbExecutor.sol.

Per episode, at its peak block:

  gas_units = execute_gas_l2 + intrinsic_and_l1_gas
  gas_usdc  = gas_units * base_fee(peak block) * ETH price (USDC per WETH, the episode's mid price)

- execute_gas_l2: L2 execution gas of execute() from the executor's Foundry gas report, saved by
  research/forktests/run.py in research/results/<forktest run>/config.json
  (gas.synthetic_execute, the one successful SYNTHETIC execute: 0.30% -> 0.05%, 50,000 USDC in).
  APPROXIMATION (labelled): one trade size and one pool pair. That trade crosses many
  initialized ticks; the small arbitrages in the replay cross few or none, so this probably
  OVERSTATES their execution gas. Forge measures the call frame only: no intrinsic gas, no
  calldata, no L1 fee.
- intrinsic_and_l1_gas: measured with an archive eth_call to Arbitrum's NodeInterface
  (0x...C8) gasEstimateComponents(to, false, calldata) AT THE PEAK BLOCK, where calldata is the
  episode's real execute(poolA, poolB, zeroForOne, amountIn, minProfit) encoding and `to` is an
  address with no code (so only intrinsic gas + the L1 data component are charged). Returns
  (gasEstimate, gasEstimateForL1, baseFee, l1BaseFeeEstimate); gasEstimate already includes the
  L1 component converted to L2 gas units.
- L1 data price at the peak block, recorded for each episode: NodeInterface's
  l1BaseFeeEstimate, plus ArbGasInfo (0x...6C) getL1BaseFeeEstimate() and getPricesInWei()
  (per-L1-calldata-byte price).

All archive reads are cached in data/cache/gas/l1_gas.parquet keyed by (block, calldata), so a
block is never queried twice for the same transaction.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import polars as pl
from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address

from data.rpc import RpcClient

ROOT = Path(__file__).resolve().parents[2]
NODE_INTERFACE = "0x00000000000000000000000000000000000000C8"
ARB_GAS_INFO = "0x000000000000000000000000000000000000006C"
PROBE_TO = "0x000000000000000000000000000000000000dEaD"  # must have no code at the block (checked)
CACHE_PATH = ROOT / "data" / "cache" / "gas" / "l1_gas.parquet"
SCHEMA = {
    "block": pl.Int64, "calldata": pl.Utf8, "gas_estimate": pl.Int64, "gas_estimate_l1": pl.Int64,
    "ni_base_fee_wei": pl.Int64, "ni_l1_base_fee_estimate_wei": pl.Int64,
    "agi_l1_base_fee_estimate_wei": pl.Int64, "agi_wei_per_l1_calldata_byte": pl.Int64,
}
EXECUTE_SIG = "execute(address,address,bool,uint256,uint256)"


def _sel(sig: str) -> bytes:
    return keccak(text=sig)[:4]


def _eth_call(client: RpcClient, to: str, data: bytes, block: int) -> bytes:
    return bytes.fromhex(client.call("eth_call", [{"to": to, "data": "0x" + data.hex()}, hex(block)])[2:])


# ---------------------------------------------------------------- execute() gas from the fork tests


def executor_gas(forktest_run: str, results_root: Path = ROOT / "research" / "results") -> dict:
    """execute() L2 gas from a saved fork-test run (research/forktests/run.py output)."""
    cfg = json.loads((results_root / forktest_run / "config.json").read_text())
    g = cfg["gas"]["synthetic_execute"]
    if g["calls"] != 1 or g["min"] != g["max"]:
        raise ValueError(f"fork-test run {forktest_run}: expected exactly one SYNTHETIC execute call, got {g}")
    return {
        "execute_gas_l2": int(g["median"]),
        "source": f"research/results/{forktest_run}/config.json gas.synthetic_execute (forge gas report)",
        "scenario": cfg["synthetic_scenario"]["execute"],
        "fork_block": cfg["fork"]["fork_block_number"],
        "git_commit": cfg["git_commit"],
        "all_execute_calls": cfg["gas"]["execute_all_calls"],
    }


def execute_calldata(pool_a: str, pool_b: str, zero_for_one: bool, amount_in: int, min_profit: int) -> str:
    return "0x" + (_sel(EXECUTE_SIG) + encode(
        ["address", "address", "bool", "uint256", "uint256"],
        [to_checksum_address(pool_a), to_checksum_address(pool_b), zero_for_one, amount_in, min_profit])).hex()


# ---------------------------------------------------------------- per-block intrinsic + L1 component


def _fetch_l1(client: RpcClient, block: int, calldata: str) -> dict:
    if len(client.call("eth_getCode", [PROBE_TO, hex(block)])) > 2:
        raise RuntimeError(f"probe target {PROBE_TO} has code at block {block}")
    data = _sel("gasEstimateComponents(address,bool,bytes)") + encode(
        ["address", "bool", "bytes"], [PROBE_TO, False, bytes.fromhex(calldata[2:])])
    gas, gas_l1, base_fee, l1_base_fee = decode(["uint64", "uint64", "uint256", "uint256"],
                                                _eth_call(client, NODE_INTERFACE, data, block))
    (agi_l1,) = decode(["uint256"], _eth_call(client, ARB_GAS_INFO, _sel("getL1BaseFeeEstimate()"), block))
    prices = decode(["uint256"] * 6, _eth_call(client, ARB_GAS_INFO, _sel("getPricesInWei()"), block))
    return {"block": block, "calldata": calldata, "gas_estimate": gas, "gas_estimate_l1": gas_l1,
            "ni_base_fee_wei": base_fee, "ni_l1_base_fee_estimate_wei": l1_base_fee,
            "agi_l1_base_fee_estimate_wei": agi_l1, "agi_wei_per_l1_calldata_byte": prices[1]}


def l1_components(client: RpcClient, requests: list[tuple[int, str]], path: Path = CACHE_PATH,
                  concurrency: int = 4) -> pl.DataFrame:
    """Rows for each (block, calldata), fetching only those not cached yet."""
    cached = pl.read_parquet(path) if path.exists() else pl.DataFrame(schema=SCHEMA)
    have = set(zip(cached["block"].to_list(), cached["calldata"].to_list()))
    missing = sorted(set(requests) - have)
    if missing:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            new = pl.DataFrame(list(ex.map(lambda r: _fetch_l1(client, *r), missing)), schema=SCHEMA)
        cached = pl.concat([cached, new]).unique(["block", "calldata"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        cached.write_parquet(tmp)
        os.replace(tmp, path)
    want = pl.DataFrame({"block": [b for b, _ in requests], "calldata": [c for _, c in requests]},
                        schema={"block": pl.Int64, "calldata": pl.Utf8})
    return want.join(cached, on=["block", "calldata"], how="left")


def episode_gas(client: RpcClient, episodes: pl.DataFrame, metas, exec_gas: dict, usdc_usd: float,
                base_fees: dict[int, int]) -> pl.DataFrame:
    """Add gas columns to `episodes` (needs pair, peak_block, cheap_pool, expensive_pool,
    size_usdc, peak_gross_usdc, price_cheap, price_expensive; *_single_tick optional).

    Columns: execute_calldata, gas_units_execute (forge), gas_units_intrinsic_l2,
    gas_units_l1 (NodeInterface, at the peak block), gas_units (sum), base_fee_wei (header),
    ni_base_fee_wei, l1_base_fee_estimate_wei, wei_per_l1_calldata_byte, peak_gas_usdc,
    peak_net_usdc (and peak_net_usdc_single_tick if the single-tick gross is present)."""
    by_addr = {m.address: m for m in metas}
    calldata = []
    for r in episodes.iter_rows(named=True):
        mc = by_addr[r["cheap_pool"]]
        amount = max(1, round(r["size_usdc"] * 10 ** mc.usdc_decimals))
        # USDC in: zeroForOne is true only if USDC is token0 of the first pool.
        calldata.append(execute_calldata(r["cheap_pool"], r["expensive_pool"], not mc.weth_is_token0, amount, 1))
    reqs = list(zip(episodes["peak_block"].to_list(), calldata))
    l1 = l1_components(client, reqs)
    out = episodes.with_columns(
        pl.Series("execute_calldata", calldata),
        pl.lit(exec_gas["execute_gas_l2"], dtype=pl.Int64).alias("gas_units_execute"),
        (l1["gas_estimate"] - l1["gas_estimate_l1"]).alias("gas_units_intrinsic_l2"),
        l1["gas_estimate_l1"].alias("gas_units_l1"),
        l1["ni_base_fee_wei"].alias("ni_base_fee_wei"),
        l1["ni_l1_base_fee_estimate_wei"].alias("l1_base_fee_estimate_wei"),
        l1["agi_l1_base_fee_estimate_wei"].alias("agi_l1_base_fee_estimate_wei"),
        l1["agi_wei_per_l1_calldata_byte"].alias("wei_per_l1_calldata_byte"),
        pl.Series("base_fee_wei", [base_fees[b] for b in episodes["peak_block"].to_list()], dtype=pl.Int64),
    ).with_columns(
        (pl.col("gas_units_execute") + pl.col("gas_units_intrinsic_l2") + pl.col("gas_units_l1")).alias("gas_units"),
    ).with_columns(
        (pl.col("gas_units") * pl.col("base_fee_wei") / 1e18
         * (pl.col("price_cheap") + pl.col("price_expensive")) / 2 * usdc_usd).alias("peak_gas_usdc"),
    ).with_columns((pl.col("peak_gross_usdc") - pl.col("peak_gas_usdc")).alias("peak_net_usdc"))
    if "peak_gross_usdc_single_tick" in out.columns:
        out = out.with_columns(
            (pl.col("peak_gross_usdc_single_tick") - pl.col("peak_gas_usdc")).alias("peak_net_usdc_single_tick"))
    return out


def sensitivity(episodes: pl.DataFrame, multipliers=(0.5, 1.0, 2.0)) -> list[dict]:
    """Net-positive episode count M and total net profit when every episode's gas cost is
    scaled by k (k = 1 is the estimate above)."""
    out = []
    for k in multipliers:
        if episodes.is_empty():
            out.append({"k": k, "M": 0, "net_usdc": 0.0})
            continue
        net = episodes["peak_gross_usdc"] - k * episodes["peak_gas_usdc"]
        out.append({"k": k, "M": int((net > 0).sum()), "net_usdc": float(net.filter(net > 0).sum())})
    return out
