"""Collect Swap and Mint/Burn logs for every registry pool in one pass.

One eth_getLogs request per chunk asks for both event types (and only the pools/types still
missing there), and each log goes to its own Parquet cache:
data/cache/swaps/... (data/collectors/swaps.py) and data/cache/liquidity_events/...
(data/collectors/liquidity_events.py). Cached ranges are never fetched again.

Usage:  uv run python -m data.collectors.pool_logs <from_block> <to_block>
  Both bounds inclusive; to_block must be finalized. Chunk size, concurrency and flush size come
  from configs/collector.yaml.
"""

from __future__ import annotations

import sys

import yaml

from data.collectors.liquidity_events import LIQUIDITY_SPEC, load_liquidity_events
from data.collectors.swaps import CONFIG_PATH, SWAP_SPEC, collect, load_swaps, resolve_head
from data.pools.registry import load_registry
from data.rpc import RpcClient, load_rpc_url, redact


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-3].strip(), file=sys.stderr)
        return 2
    lo, hi = int(argv[0]), int(argv[1])
    if lo > hi:
        print("from_block must be <= to_block", file=sys.stderr)
        return 2
    url = load_rpc_url()
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    registry = load_registry()
    pools = [p["address"] for p in registry["pools"]]
    client = RpcClient(url, max_retries=cfg.get("rpc_max_retries", 8))
    try:
        if int(client.call("eth_chainId", []), 16) != registry["chain_id"]:
            raise RuntimeError("RPC chain id does not match the registry")
        resolve_head(client, hi)  # raises unless hi is finalized
        print(f"window: blocks {lo}..{hi} ({hi - lo + 1} blocks), {len(pools)} pools, Swap + Mint/Burn")
        n = collect(client, pools, (lo, hi), cfg, specs=[SWAP_SPEC, LIQUIDITY_SPEC])
        print(f"fetched {n} chunks ({client.calls} RPC calls incl. retries)" if n else "all ranges already cached")
    except Exception as e:  # never let the RPC URL leak through an error message
        print(f"collector failed: {redact(e, url)}", file=sys.stderr)
        return 1
    for p in registry["pools"]:
        sw = load_swaps(p["address"], (lo, hi))
        liq = load_liquidity_events(p["address"], (lo, hi))
        print(f"  {p['label']:>9}  {p['address']}  {sw.height:>7} swaps  {liq.height:>6} mint/burn")
    return 0


if __name__ == "__main__":
    sys.exit(main())
