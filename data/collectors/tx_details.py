"""Transaction details (sender, to, index, gas used) for chosen transactions, cached as Parquet.

Used for the transactions that closed transaction-level episodes. Per tx hash:
eth_getTransactionByHash (from, to, transactionIndex, blockNumber) and
eth_getTransactionReceipt (gasUsed, effectiveGasPrice, status, and Arbitrum's gasUsedForL1).
Finalized transactions never change, so each hash is fetched once and stored in
data/cache/tx_details/tx_details.parquet.

`gas_used` is the receipt's total gasUsed. On Arbitrum it already includes `gas_used_for_l1`
(the L1 data component expressed in L2 gas units); `gas_used_l2` = gas_used - gas_used_for_l1.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import polars as pl
from eth_utils import keccak, to_checksum_address

from data.rpc import RpcClient

ROOT = Path(__file__).resolve().parents[2]
TX_DETAILS_PATH = ROOT / "data" / "cache" / "tx_details" / "tx_details.parquet"
SCHEMA = {
    "tx_hash": pl.Utf8, "block_number": pl.Int64, "transaction_index": pl.Int32, "sender": pl.Utf8, "to": pl.Utf8,
    "gas_used": pl.Int64, "gas_used_for_l1": pl.Int64, "effective_gas_price_wei": pl.Int64, "status": pl.Int32,
    "input_selector": pl.Utf8,
}


def _hex(v) -> int | None:
    return int(v, 16) if v is not None else None


def fetch_tx_details(client: RpcClient, h: str) -> dict:
    tx = client.call("eth_getTransactionByHash", [h])
    rc = client.call("eth_getTransactionReceipt", [h])
    if tx is None or rc is None or tx.get("blockNumber") is None:
        raise RuntimeError(f"transaction {h} not found or not mined")
    inp = tx.get("input") or "0x"
    return {
        "tx_hash": h.lower(),
        "block_number": _hex(tx["blockNumber"]),
        "transaction_index": _hex(tx["transactionIndex"]),
        "sender": to_checksum_address(tx["from"]),
        "to": to_checksum_address(tx["to"]) if tx.get("to") else None,  # None = contract creation
        "gas_used": _hex(rc["gasUsed"]),
        "gas_used_for_l1": _hex(rc.get("gasUsedForL1")),
        "effective_gas_price_wei": _hex(rc.get("effectiveGasPrice")),
        "status": _hex(rc.get("status")),
        "input_selector": inp[:10] if len(inp) >= 10 else None,
    }


def get_tx_details(client: RpcClient, tx_hashes: list[str], path: Path = TX_DETAILS_PATH,
                   concurrency: int = 4) -> pl.DataFrame:
    """Rows for `tx_hashes` (lowercase), fetching only the hashes not cached yet."""
    want = {h.lower() for h in tx_hashes if h}
    cached = pl.read_parquet(path) if path.exists() else pl.DataFrame(schema=SCHEMA)
    missing = sorted(want - set(cached["tx_hash"].to_list()))
    if missing:
        print(f"  fetching details of {len(missing)} transactions", flush=True)
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            new = pl.DataFrame(list(ex.map(lambda h: fetch_tx_details(client, h), missing)), schema=SCHEMA)
        cached = pl.concat([cached, new]).unique("tx_hash").sort(["block_number", "transaction_index"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        cached.write_parquet(tmp)
        os.replace(tmp, path)
    return cached.filter(pl.col("tx_hash").is_in(list(want)))


# --- Swap venues in a transaction's receipt (for classifying closers) -------------------------
# Swap event signatures recognised in receipts. The topics are computed from the signatures here,
# not copied from memory. The list is NOT exhaustive: venues with other event shapes (Curve,
# order-book, RFQ fills, ...) are not counted, so `n_swap_pools` is a lower bound.
SWAP_EVENT_SIGNATURES = {
    # Uniswap v3 and forks with the same event (SushiSwap v3, Algebra/Camelot v3 share it).
    "v3": "Swap(address,address,int256,int256,uint160,uint128,int24)",
    "pancake_v3": "Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)",
    "v2": "Swap(address,uint256,uint256,uint256,uint256,address)",
    # Singleton venues: the pool is identified by topic1 (pool id), not by the emitting address.
    "v4": "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)",
    "balancer_v2": "Swap(bytes32,address,address,uint256,uint256)",
}
SWAP_EVENT_TOPICS = {"0x" + keccak(text=s).hex(): k for k, s in SWAP_EVENT_SIGNATURES.items()}
SINGLETON_KINDS = {"v4", "balancer_v2"}
TX_SWAPS_PATH = ROOT / "data" / "cache" / "tx_details" / "tx_swaps.parquet"
SWAPS_SCHEMA = {"tx_hash": pl.Utf8, "n_logs": pl.Int32, "swap_pools": pl.List(pl.Utf8), "swap_kinds": pl.List(pl.Utf8)}


def receipt_swap_pools(receipt: dict) -> tuple[list[str], list[str]]:
    """Distinct pools that emitted a recognised Swap event, in log order, with their kind.
    v2/v3 pools are keyed by lowercase address; singleton venues by '<address>:<pool id>'."""
    pools, kinds = [], []
    for log in receipt.get("logs", []):
        topics = log.get("topics") or []
        kind = SWAP_EVENT_TOPICS.get(topics[0].lower()) if topics else None
        if kind is None:
            continue
        key = log["address"].lower()
        if kind in SINGLETON_KINDS:
            key += ":" + topics[1].lower()
        if key not in pools:
            pools.append(key)
            kinds.append(kind)
    return pools, kinds


def get_tx_swaps(client: RpcClient, tx_hashes: list[str], path: Path = TX_SWAPS_PATH,
                 concurrency: int = 4) -> pl.DataFrame:
    """Per tx hash: number of logs and the distinct swap pools in its receipt. Cached."""
    want = {h.lower() for h in tx_hashes if h}
    cached = pl.read_parquet(path) if path.exists() else pl.DataFrame(schema=SWAPS_SCHEMA)
    missing = sorted(want - set(cached["tx_hash"].to_list()))
    if missing:
        print(f"  fetching receipts of {len(missing)} transactions for swap venues", flush=True)

        def one(h):
            rc = client.call("eth_getTransactionReceipt", [h])
            if rc is None:
                raise RuntimeError(f"no receipt for {h}")
            pools, kinds = receipt_swap_pools(rc)
            return {"tx_hash": h, "n_logs": len(rc.get("logs", [])), "swap_pools": pools, "swap_kinds": kinds}

        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            new = pl.DataFrame(list(ex.map(one, missing)), schema=SWAPS_SCHEMA)
        cached = pl.concat([cached, new]).unique("tx_hash").sort("tx_hash")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        cached.write_parquet(tmp)
        os.replace(tmp, path)
    return cached.filter(pl.col("tx_hash").is_in(list(want)))
