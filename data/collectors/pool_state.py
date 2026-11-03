"""Pool state snapshot (slot0 sqrtPriceX96/tick and in-range liquidity) at a given block.

Used to seed the replay at the block *before* the window: a pool that does not swap early in
the window still needs a known state. Snapshots are cached as Parquet in
data/cache/pool_state/<pool>/<block>.parquet (one row) and never fetched twice.
"""

from __future__ import annotations

import os
from pathlib import Path

import polars as pl
from web3 import Web3

from data.abi import UNISWAP_V3_POOL

ROOT = Path(__file__).resolve().parents[2]
STATE_ROOT = ROOT / "data" / "cache" / "pool_state"
SCHEMA = {"pool": pl.Utf8, "block_number": pl.Int64, "sqrt_price_x96": pl.Utf8, "liquidity": pl.Utf8, "tick": pl.Int32}


def get_pool_state(w3: Web3, pool: str, block: int, root: Path = STATE_ROOT) -> dict:
    path = root / pool.lower() / f"{block:012d}.parquet"
    if path.exists():
        return pl.read_parquet(path).row(0, named=True)
    c = w3.eth.contract(address=Web3.to_checksum_address(pool), abi=UNISWAP_V3_POOL)
    slot0 = c.functions.slot0().call(block_identifier=block)
    liquidity = c.functions.liquidity().call(block_identifier=block)
    row = {
        "pool": Web3.to_checksum_address(pool),
        "block_number": block,
        "sqrt_price_x96": str(slot0[0]),
        "liquidity": str(liquidity),
        "tick": slot0[1],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    pl.DataFrame([row], schema=SCHEMA).write_parquet(tmp)
    os.replace(tmp, path)
    return row
