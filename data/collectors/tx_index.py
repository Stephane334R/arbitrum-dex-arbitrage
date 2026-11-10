"""Transaction index lookup (tx hash -> block number, index in block), cached as Parquet.

The swap cache predates the transaction-level replay and stores `log_index` but not
`transactionIndex`. Rather than re-fetch the same Swap logs (the cache rule forbids it), the
index of each swap transaction is looked up once with eth_getTransactionByHash and stored in
data/cache/tx_index/tx_index.parquet. Finalized transactions never move, so a cached row is
never fetched again.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import polars as pl

from data.rpc import RpcClient

ROOT = Path(__file__).resolve().parents[2]
TX_INDEX_PATH = ROOT / "data" / "cache" / "tx_index" / "tx_index.parquet"
SCHEMA = {"tx_hash": pl.Utf8, "block_number": pl.Int64, "transaction_index": pl.Int32}


def get_tx_indices(client: RpcClient, tx_hashes: list[str], path: Path = TX_INDEX_PATH,
                   concurrency: int = 4) -> pl.DataFrame:
    """Rows (tx_hash lowercase, block_number, transaction_index) for `tx_hashes`, fetching only
    the hashes not cached yet."""
    want = {h.lower() for h in tx_hashes}
    cached = pl.read_parquet(path) if path.exists() else pl.DataFrame(schema=SCHEMA)
    missing = sorted(want - set(cached["tx_hash"].to_list()))
    if missing:

        def fetch(h: str) -> dict:
            tx = client.call("eth_getTransactionByHash", [h])
            if tx is None or tx.get("blockNumber") is None:
                raise RuntimeError(f"transaction {h} not found or not mined")
            return {"tx_hash": h, "block_number": int(tx["blockNumber"], 16),
                    "transaction_index": int(tx["transactionIndex"], 16)}

        print(f"  looking up {len(missing)} transaction indices", flush=True)
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            new = pl.DataFrame(list(ex.map(fetch, missing)), schema=SCHEMA)
        cached = pl.concat([cached, new]).unique("tx_hash").sort(["block_number", "transaction_index"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        cached.write_parquet(tmp)
        os.replace(tmp, path)
    return cached.filter(pl.col("tx_hash").is_in(list(want)))
