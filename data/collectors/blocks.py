"""Block header cache: timestamp and baseFeePerGas per block number.

Headers for finalized blocks never change, so each block is fetched once and stored in
data/cache/blocks/headers.parquet.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import polars as pl

from data.rpc import RpcClient

ROOT = Path(__file__).resolve().parents[2]
HEADERS_PATH = ROOT / "data" / "cache" / "blocks" / "headers.parquet"
SCHEMA = {"block_number": pl.Int64, "timestamp": pl.Int64, "base_fee_per_gas": pl.Int64}


def _load(path: Path) -> pl.DataFrame:
    return pl.read_parquet(path) if path.exists() else pl.DataFrame(schema=SCHEMA)


def get_headers(client: RpcClient, blocks: list[int], path: Path = HEADERS_PATH, concurrency: int = 4) -> pl.DataFrame:
    """Return headers for `blocks`, fetching only the ones not cached yet."""
    cached = _load(path)
    have = set(cached["block_number"].to_list())
    missing = sorted(set(blocks) - have)
    if missing:

        def fetch(n: int) -> dict:
            h = client.call("eth_getBlockByNumber", [hex(n), False])
            return {
                "block_number": int(h["number"], 16),
                "timestamp": int(h["timestamp"], 16),
                "base_fee_per_gas": int(h["baseFeePerGas"], 16),
            }

        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            new = pl.DataFrame(list(ex.map(fetch, missing)), schema=SCHEMA)
        cached = pl.concat([cached, new]).unique("block_number").sort("block_number")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        cached.write_parquet(tmp)
        os.replace(tmp, path)
    return cached.filter(pl.col("block_number").is_in(list(set(blocks)))).sort("block_number")


def first_block_at_or_after(client: RpcClient, ts: int, lo: int, hi: int, path: Path = HEADERS_PATH) -> int:
    """Smallest block in [lo, hi] whose timestamp is >= ts (block timestamps are non-decreasing).
    Binary search over cached headers: about log2(hi - lo) header fetches, each cached."""
    def t(n: int) -> int:
        return get_headers(client, [n], path).row(0, named=True)["timestamp"]

    if t(hi) < ts:
        raise ValueError(f"block {hi} is before timestamp {ts}")
    while lo < hi:
        mid = (lo + hi) // 2
        if t(mid) >= ts:
            hi = mid
        else:
            lo = mid + 1
    return lo
