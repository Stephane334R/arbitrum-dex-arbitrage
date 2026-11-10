"""Mint and Burn log collector for the verified pools in configs/pools.yaml.

Same cache layout and fetch logic as the swap collector (data/collectors/swaps.py, through its
`LogSpec`): data/cache/liquidity_events/<pool>/<from>_<to>.parquet, inclusive block ranges,
empty ranges written too, so no (pool, block range) is ever fetched twice. One eth_getLogs
request asks for both event signatures (topic0 = [Mint, Burn]).

Events (Uniswap v3 core, UniswapV3Pool.sol):
  Mint(address sender, address indexed owner, int24 indexed tickLower, int24 indexed tickUpper,
       uint128 amount, uint256 amount0, uint256 amount1)
  Burn(address indexed owner, int24 indexed tickLower, int24 indexed tickUpper,
       uint128 amount, uint256 amount0, uint256 amount1)
`amount` is the liquidity added or removed. The pool adds it to (Mint) or subtracts it from
(Burn) the in-range liquidity only if tickLower <= slot0.tick < tickUpper at that moment
(UniswapV3Pool._modifyPosition). A Burn with amount 0 is a fee "poke" and changes nothing.

Unlike the swap cache, rows keep `transaction_index` (from the log), which the
transaction-level replay sorts on.

Usage:  uv run python -m data.collectors.liquidity_events [head_block]
  (same window semantics as the swap collector: configs/collector.yaml `blocks`, ending at
  `head` or at the block given on the command line)
"""

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl
import yaml
from eth_abi import decode as abi_decode
from eth_utils import keccak, to_checksum_address

from data.collectors.swaps import CONFIG_PATH, LogSpec, collect, load_cached, resolve_head
from data.pools.registry import load_registry
from data.rpc import RpcClient, load_rpc_url, redact

ROOT = Path(__file__).resolve().parents[2]
CACHE_ROOT = ROOT / "data" / "cache" / "liquidity_events"

MINT_SIGNATURE = "Mint(address,address,int24,int24,uint128,uint256,uint256)"
BURN_SIGNATURE = "Burn(address,int24,int24,uint128,uint256,uint256)"
MINT_TOPIC = "0x" + keccak(text=MINT_SIGNATURE).hex()
BURN_TOPIC = "0x" + keccak(text=BURN_SIGNATURE).hex()

SCHEMA = {
    "pool": pl.Utf8,
    "block_number": pl.Int64,
    "block_timestamp": pl.Int64,
    "tx_hash": pl.Utf8,
    "transaction_index": pl.Int32,
    "log_index": pl.Int32,
    "event": pl.Utf8,  # "mint" | "burn"
    "owner": pl.Utf8,
    "tick_lower": pl.Int32,
    "tick_upper": pl.Int32,
    "amount": pl.Utf8,  # uint128 liquidity, base-10 string (exact)
    "amount0": pl.Utf8,
    "amount1": pl.Utf8,
}


def _topic_int24(topic: str) -> int:
    return abi_decode(["int24"], bytes.fromhex(topic[2:]))[0]


def decode_liquidity_log(log: dict) -> dict:
    """Decode one raw eth_getLogs entry for a Mint or Burn event."""
    topics = log["topics"]
    t0 = topics[0].lower()
    data = bytes.fromhex(log["data"][2:])
    if t0 == MINT_TOPIC:
        _sender, amount, amount0, amount1 = abi_decode(["address", "uint128", "uint256", "uint256"], data)
        event = "mint"
    elif t0 == BURN_TOPIC:
        amount, amount0, amount1 = abi_decode(["uint128", "uint256", "uint256"], data)
        event = "burn"
    else:
        raise ValueError(f"not a Mint/Burn log: topic0={topics[0]}")
    if "blockTimestamp" not in log:
        raise KeyError("log has no blockTimestamp")
    return {
        "pool": to_checksum_address(log["address"]),
        "block_number": int(log["blockNumber"], 16),
        "block_timestamp": int(log["blockTimestamp"], 16),
        "tx_hash": log["transactionHash"],
        "transaction_index": int(log["transactionIndex"], 16),
        "log_index": int(log["logIndex"], 16),
        "event": event,
        "owner": to_checksum_address("0x" + topics[1][-40:]),
        "tick_lower": _topic_int24(topics[2]),
        "tick_upper": _topic_int24(topics[3]),
        "amount": str(amount),
        "amount0": str(amount0),
        "amount1": str(amount1),
    }


LIQUIDITY_SPEC = LogSpec((MINT_TOPIC, BURN_TOPIC), decode_liquidity_log, SCHEMA, CACHE_ROOT)


def load_liquidity_events(pool: str, rng: tuple[int, int] | None = None, cache_root: Path = CACHE_ROOT) -> pl.DataFrame:
    return load_cached(pool, rng, cache_root, SCHEMA)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    url = load_rpc_url()
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    if argv:
        cfg["head"] = int(argv[0])
    registry = load_registry()
    pools = [p["address"] for p in registry["pools"]]
    client = RpcClient(url)
    try:
        if int(client.call("eth_chainId", []), 16) != registry["chain_id"]:
            raise RuntimeError("RPC chain id does not match the registry")
        end = resolve_head(client, cfg["head"])
        window = (end - cfg["blocks"] + 1, end)
        print(f"window: blocks {window[0]}..{window[1]} ({cfg['blocks']} blocks, head={cfg['head']})")
        n = collect(client, pools, window, cfg, spec=LIQUIDITY_SPEC)
        print(f"fetched {n} chunks ({client.calls} RPC calls incl. retries)" if n else "all ranges already cached")
    except Exception as e:  # never let the RPC URL leak through an error message
        print(f"collector failed: {redact(e, url)}", file=sys.stderr)
        return 1

    print("Mint/Burn events per pool in window:")
    for p in registry["pools"]:
        df = load_liquidity_events(p["address"], window)
        n_mint = int((df["event"] == "mint").sum()) if df.height else 0
        print(f"  fee={p['fee']:>5}  {p['address']}  {n_mint:>5} mints  {df.height - n_mint:>5} burns")
    return 0


if __name__ == "__main__":
    sys.exit(main())
