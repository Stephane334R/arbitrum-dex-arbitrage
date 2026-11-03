"""Swap log collector for the verified pools in configs/pools.yaml.

Fetches Uniswap v3 `Swap(address,address,int256,int256,uint160,uint128,int24)` events and
caches them as Parquet under data/cache/swaps/<pool>/<from>_<to>.parquet (inclusive block
range, zero-padded). The file names are the cache index: a block range that is covered by a
file for a pool is never fetched again for that pool. Ranges with no swaps are still written
(as empty files) so they are not refetched.

Large integers (amounts are int256, sqrtPriceX96 is uint160, liquidity is uint128) do not
fit in Parquet/Polars 64-bit integer types, so they are stored as exact base-10 strings.
Convert them with Python `int()` (exact), not float.

Usage:  uv run python -m data.collectors.swaps
"""

from __future__ import annotations

import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl
import yaml
from eth_abi import decode as abi_decode
from eth_utils import keccak, to_checksum_address

from data.pools.registry import load_registry
from data.rpc import RpcClient, RpcError, load_rpc_url, redact

ROOT = Path(__file__).resolve().parents[2]
CACHE_ROOT = ROOT / "data" / "cache" / "swaps"
CONFIG_PATH = ROOT / "configs" / "collector.yaml"

SWAP_SIGNATURE = "Swap(address,address,int256,int256,uint160,uint128,int24)"
SWAP_TOPIC = "0x" + keccak(text=SWAP_SIGNATURE).hex()

SCHEMA = {
    "pool": pl.Utf8,
    "block_number": pl.Int64,
    "block_timestamp": pl.Int64,
    "tx_hash": pl.Utf8,
    "log_index": pl.Int32,
    "sender": pl.Utf8,
    "recipient": pl.Utf8,
    "amount0": pl.Utf8,
    "amount1": pl.Utf8,
    "sqrt_price_x96": pl.Utf8,
    "liquidity": pl.Utf8,
    "tick": pl.Int32,
}

_FILE_RE = re.compile(r"^(\d+)_(\d+)\.parquet$")

Range = tuple[int, int]  # inclusive [from, to]


# ---------------------------------------------------------------- decoding


def decode_swap_log(log: dict) -> dict:
    """Decode one raw eth_getLogs entry for a Swap event."""
    topics = log["topics"]
    if topics[0].lower() != SWAP_TOPIC:
        raise ValueError(f"not a Swap log: topic0={topics[0]}")
    amount0, amount1, sqrt_price_x96, liquidity, tick = abi_decode(
        ["int256", "int256", "uint160", "uint128", "int24"], bytes.fromhex(log["data"][2:])
    )
    if "blockTimestamp" not in log:
        raise KeyError("log has no blockTimestamp")
    return {
        "pool": to_checksum_address(log["address"]),
        "block_number": int(log["blockNumber"], 16),
        "block_timestamp": int(log["blockTimestamp"], 16),
        "tx_hash": log["transactionHash"],
        "log_index": int(log["logIndex"], 16),
        "sender": to_checksum_address("0x" + topics[1][-40:]),
        "recipient": to_checksum_address("0x" + topics[2][-40:]),
        "amount0": str(amount0),
        "amount1": str(amount1),
        "sqrt_price_x96": str(sqrt_price_x96),
        "liquidity": str(liquidity),
        "tick": tick,
    }


# ---------------------------------------------------------------- cache index


def pool_cache_dir(pool: str, cache_root: Path = CACHE_ROOT) -> Path:
    return cache_root / pool.lower()


def covered_ranges(pool_dir: Path) -> list[Range]:
    if not pool_dir.exists():
        return []
    out = []
    for f in pool_dir.iterdir():
        m = _FILE_RE.match(f.name)
        if m:
            out.append((int(m.group(1)), int(m.group(2))))
    return sorted(out)


def missing_ranges(want: Range, covered: list[Range]) -> list[Range]:
    """Parts of `want` not covered by any range in `covered` (all inclusive)."""
    lo, hi = want
    gaps, cursor = [], lo
    for a, b in sorted(covered):
        if b < cursor:
            continue
        if a > hi:
            break
        if a > cursor:
            gaps.append((cursor, a - 1))
        cursor = max(cursor, b + 1)
        if cursor > hi:
            break
    if cursor <= hi:
        gaps.append((cursor, hi))
    return gaps


def plan_fetches(gaps_by_pool: dict[str, list[Range]], max_block_range: int) -> list[tuple[int, int, tuple[str, ...]]]:
    """Split all pools' gaps into eth_getLogs chunks. Each chunk lists exactly the pools that
    are missing that whole chunk, so one request serves several pools without refetching any
    pool's cached range."""
    cuts = set()
    for gaps in gaps_by_pool.values():
        for a, b in gaps:
            cuts.update((a, b + 1))
    cuts = sorted(cuts)
    chunks = []
    for start, nxt in zip(cuts, cuts[1:]):
        end = nxt - 1
        pools = tuple(p for p, gaps in gaps_by_pool.items() if any(a <= start and end <= b for a, b in gaps))
        if not pools:
            continue
        for a in range(start, end + 1, max_block_range):
            chunks.append((a, min(a + max_block_range - 1, end), pools))
    return chunks


def write_cache_file(pool_dir: Path, rng: Range, rows: list[dict]) -> Path:
    pool_dir.mkdir(parents=True, exist_ok=True)
    df = pl.DataFrame(rows, schema=SCHEMA).sort(["block_number", "log_index"])
    path = pool_dir / f"{rng[0]:012d}_{rng[1]:012d}.parquet"
    tmp = path.with_suffix(".parquet.tmp")
    df.write_parquet(tmp)
    os.replace(tmp, path)  # atomic: a crash never leaves a half-written file that looks complete
    return path


def load_swaps(pool: str, rng: Range | None = None, cache_root: Path = CACHE_ROOT) -> pl.DataFrame:
    pool_dir = pool_cache_dir(pool, cache_root)
    files = [pool_dir / f"{a:012d}_{b:012d}.parquet" for a, b in covered_ranges(pool_dir)]
    if not files:
        return pl.DataFrame(schema=SCHEMA)
    df = pl.concat([pl.read_parquet(f) for f in files])
    if rng is not None:
        df = df.filter(pl.col("block_number").is_between(rng[0], rng[1]))
    return df.unique(["block_number", "log_index"]).sort(["block_number", "log_index"])


# ---------------------------------------------------------------- fetching


def _is_range_error(e: RpcError) -> bool:
    """Heuristic: providers word "range/result too large" differently (Alchemy, Infura,
    QuickNode, Nitro). Match on common phrases rather than codes, which overlap with other
    invalid-request errors."""
    m = e.message.lower()
    return any(s in m for s in ("block range", "response size", "too many", "query returned more than", "limit exceeded", "range is too large"))


def fetch_logs(client: RpcClient, a: int, b: int, pools: tuple[str, ...]) -> list[dict]:
    """eth_getLogs for [a, b]; on a range/size error, split the range in half and retry."""
    try:
        return client.call(
            "eth_getLogs",
            [{"fromBlock": hex(a), "toBlock": hex(b), "address": list(pools), "topics": [SWAP_TOPIC]}],
        )
    except RpcError as e:
        if not _is_range_error(e) or a == b:
            raise
        mid = (a + b) // 2
        return fetch_logs(client, a, mid, pools) + fetch_logs(client, mid + 1, b, pools)


@dataclass
class _Buffer:
    rng: Range
    rows: list[dict] = field(default_factory=list)


def resolve_head(client: RpcClient, head: str) -> int:
    if head not in ("finalized", "safe", "latest"):
        raise ValueError(f"head must be finalized|safe|latest, got {head!r}")
    return int(client.call("eth_getBlockByNumber", [head, False])["number"], 16)


def collect(client: RpcClient, pools: list[str], window: Range, cfg: dict, cache_root: Path = CACHE_ROOT) -> int:
    """Fetch every missing (pool, block) range in `window` and write it to the cache.
    Returns the number of eth_getLogs chunks fetched."""
    gaps = {p: missing_ranges(window, covered_ranges(pool_cache_dir(p, cache_root))) for p in pools}
    gaps = {p: g for p, g in gaps.items() if g}
    chunks = plan_fetches(gaps, cfg["max_block_range"])
    if not chunks:
        return 0

    buffers: dict[str, _Buffer] = {}

    def flush(p: str) -> None:
        buf = buffers.pop(p, None)
        if buf is not None:
            write_cache_file(pool_cache_dir(p, cache_root), buf.rng, buf.rows)

    t0, done = time.monotonic(), 0
    try:
        with ThreadPoolExecutor(max_workers=cfg["concurrency"]) as ex:
            results = ex.map(lambda c: fetch_logs(client, *c), chunks)  # yields in chunk order
            for (a, b, chunk_pools), logs in zip(chunks, results):
                by_pool: dict[str, list[dict]] = {p: [] for p in chunk_pools}
                for log in logs:
                    if log.get("removed"):
                        raise RuntimeError(f"removed (reorged) log at block {int(log['blockNumber'], 16)}")
                    row = decode_swap_log(log)
                    by_pool[row["pool"]].append(row)
                for p in chunk_pools:
                    buf = buffers.get(p)
                    if buf is not None and buf.rng[1] + 1 != a:
                        flush(p)
                        buf = None
                    if buf is None:
                        buf = buffers[p] = _Buffer((a, a - 1))
                    buf.rng = (buf.rng[0], b)
                    buf.rows.extend(by_pool[p])
                    if buf.rng[1] - buf.rng[0] + 1 >= cfg["flush_blocks"]:
                        flush(p)
                done += 1
                if done % 200 == 0 or done == len(chunks):
                    rate = done / (time.monotonic() - t0)
                    print(f"  {done}/{len(chunks)} chunks ({rate:.1f}/s)", flush=True)
    finally:
        # Buffers only ever contain fully fetched, contiguous chunks, so saving them on an
        # error or Ctrl-C is safe and keeps progress.
        for p in list(buffers):
            flush(p)
    return len(chunks)


def main() -> int:
    url = load_rpc_url()
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    registry = load_registry()
    pools = [p["address"] for p in registry["pools"]]
    client = RpcClient(url)
    try:
        if int(client.call("eth_chainId", []), 16) != registry["chain_id"]:
            raise RuntimeError("RPC chain id does not match the registry")
        end = resolve_head(client, cfg["head"])
        window = (end - cfg["blocks"] + 1, end)
        print(f"window: blocks {window[0]}..{window[1]} ({cfg['blocks']} blocks, head={cfg['head']})")
        n = collect(client, pools, window, cfg)
        print(f"fetched {n} chunks ({client.calls} RPC calls incl. retries)" if n else "all ranges already cached")
    except Exception as e:  # never let the RPC URL leak through an error message
        print(f"collector failed: {redact(e, url)}", file=sys.stderr)
        return 1

    print("swaps per pool in window:")
    for p in registry["pools"]:
        df = load_swaps(p["address"], window)
        print(f"  fee={p['fee']:>5}  {p['address']}  {df.height:>7} swaps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
