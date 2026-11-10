"""Initialized-tick snapshot of a Uniswap v3 pool at one block: slot0, liquidity, fee,
tickSpacing, and every tickBitmap word in a window around the current tick, with
liquidityGross/liquidityNet for each initialized tick in those words.

Source: the pool's own public getters `tickBitmap(int16)` and `ticks(int24)`, read with
eth_call at a pinned block. No TickLens or other helper contract is used, so there is no extra
address to verify, and the bitmap words (needed for exact swap-step boundaries, see
data/pricing/v3_swap.py) come straight from storage.

Cache (Parquet, never fetched twice), under data/cache/ticks/<pool>/<block>/:
  state.parquet          one row: sqrtPriceX96, tick, liquidity, fee, tickSpacing
  word_<pos>.parquet     one row per initialized tick in that bitmap word (zero rows = empty
                         word). The file's existence means "this word was read at this block",
                         so widening the window only fetches the new words.
Big integers are stored as base-10 strings (they do not fit in 64-bit Parquet types).

Consistency checks on every fetch: each bit set in the bitmap must have ticks().initialized ==
True and liquidityGross > 0, and must sit on a multiple of tickSpacing.

Batching: the ticks() reads of one bitmap word go through Multicall3.aggregate3 in one eth_call
(a 0.05%-pool word has up to 256 initialized ticks). Multicall3's address is verified on first
use at each block range: it must have code and getChainId() must return 42161. Each word's first
tick is also read directly and must equal the batched result.

Usage:  uv run python -m data.collectors.ticks <pool_address> <block> [words_each_side]
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import polars as pl
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from data.pricing.v3_swap import PoolSnapshot, compress, position
from data.rpc import RpcClient, load_rpc_url, redact

ROOT = Path(__file__).resolve().parents[2]
TICKS_ROOT = ROOT / "data" / "cache" / "ticks"

STATE_SCHEMA = {
    "pool": pl.Utf8,
    "block_number": pl.Int64,
    "sqrt_price_x96": pl.Utf8,
    "tick": pl.Int32,
    "liquidity": pl.Utf8,
    "fee": pl.Int32,
    "tick_spacing": pl.Int32,
}
WORD_SCHEMA = {"tick": pl.Int32, "liquidity_gross": pl.Utf8, "liquidity_net": pl.Utf8}


def _selector(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()[:8]


SEL_SLOT0 = _selector("slot0()")
SEL_LIQUIDITY = _selector("liquidity()")
SEL_FEE = _selector("fee()")
SEL_TICK_SPACING = _selector("tickSpacing()")
SEL_TICK_BITMAP = _selector("tickBitmap(int16)")
SEL_TICKS = _selector("ticks(int24)")
SEL_AGGREGATE3 = _selector("aggregate3((address,bool,bytes)[])")
SEL_GET_CHAIN_ID = _selector("getChainId()")

# Multicall3's canonical deterministic-deployment address. Not trusted: verify_multicall checks it.
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
MULTICALL_BATCH = 128  # ticks() calls per aggregate3 (well under the eth_call gas cap)
_verified_multicall_blocks: list[int] = []

SLOT0_TYPES = ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"]
TICKS_TYPES = ["uint128", "int128", "uint256", "uint256", "int56", "uint160", "uint32", "bool"]


class TickDataError(Exception):
    pass


def _call(rpc: RpcClient, to: str, data: str, block: int, types: list[str]) -> tuple:
    raw = rpc.call("eth_call", [{"to": to, "data": data}, hex(block)])
    return abi_decode(types, bytes.fromhex(raw[2:]))


def _atomic_write(df: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    df.write_parquet(tmp)
    os.replace(tmp, path)


def _word_path(base: Path, word_pos: int) -> Path:
    return base / f"word_{word_pos:+06d}.parquet"  # e.g. word_-00012.parquet


def _fetch_state(rpc: RpcClient, pool: str, block: int) -> dict:
    slot0 = _call(rpc, pool, SEL_SLOT0, block, SLOT0_TYPES)
    (liquidity,) = _call(rpc, pool, SEL_LIQUIDITY, block, ["uint128"])
    (fee,) = _call(rpc, pool, SEL_FEE, block, ["uint24"])
    (spacing,) = _call(rpc, pool, SEL_TICK_SPACING, block, ["int24"])
    return {
        "pool": pool,
        "block_number": block,
        "sqrt_price_x96": str(slot0[0]),
        "tick": slot0[1],
        "liquidity": str(liquidity),
        "fee": fee,
        "tick_spacing": spacing,
    }


def verify_multicall(rpc: RpcClient, block: int) -> None:
    """Multicall3 must have code at `block` and report Arbitrum One's chain id. Verified once per
    process for the earliest block seen (code, once deployed, stays at the address)."""
    if _verified_multicall_blocks and block >= min(_verified_multicall_blocks):
        return
    code = rpc.call("eth_getCode", [MULTICALL3, hex(block)])
    if len(code) <= 2:
        raise TickDataError(f"Multicall3 {MULTICALL3}: no code at block {block}")
    (chain_id,) = _call(rpc, MULTICALL3, SEL_GET_CHAIN_ID, block, ["uint256"])
    if chain_id != 42161:
        raise TickDataError(f"Multicall3 getChainId() = {chain_id}, expected 42161")
    _verified_multicall_blocks.append(block)


def _multicall(rpc: RpcClient, calls: list[tuple[str, str]], block: int) -> list[bytes]:
    """aggregate3 with allowFailure = false: returns each call's return data, in order."""
    payload = [(to, False, bytes.fromhex(data[2:])) for to, data in calls]
    data = SEL_AGGREGATE3 + abi_encode(["(address,bool,bytes)[]"], [payload]).hex()
    raw = rpc.call("eth_call", [{"to": MULTICALL3, "data": data}, hex(block)])
    (results,) = abi_decode(["(bool,bytes)[]"], bytes.fromhex(raw[2:]))
    if len(results) != len(calls) or not all(ok for ok, _ in results):
        raise TickDataError(f"Multicall3 aggregate3 at block {block}: a call failed")
    return [ret for _, ret in results]


def _ticks_batched(rpc: RpcClient, pool: str, block: int, ticks: list[int]) -> list[tuple]:
    verify_multicall(rpc, block)
    out = []
    for i in range(0, len(ticks), MULTICALL_BATCH):
        batch = ticks[i:i + MULTICALL_BATCH]
        rets = _multicall(rpc, [(pool, SEL_TICKS + abi_encode(["int24"], [t]).hex()) for t in batch], block)
        out += [abi_decode(TICKS_TYPES, r) for r in rets]
    if ticks:  # spot check: the first tick read directly must equal the batched value
        direct = _call(rpc, pool, SEL_TICKS + abi_encode(["int24"], [ticks[0]]).hex(), block, TICKS_TYPES)
        if tuple(direct) != tuple(out[0]):
            raise TickDataError(f"{pool} @ {block}: Multicall3 ticks({ticks[0]}) differs from a direct call")
    return out


def _fetch_word(rpc: RpcClient, pool: str, block: int, word_pos: int, spacing: int) -> list[dict]:
    data = SEL_TICK_BITMAP + abi_encode(["int16"], [word_pos]).hex()
    (bitmap,) = _call(rpc, pool, data, block, ["uint256"])
    ticks = [(word_pos * 256 + bit) * spacing for bit in range(256) if (bitmap >> bit) & 1]
    rows = []
    for tick, t in zip(ticks, _ticks_batched(rpc, pool, block, ticks)):
        gross, net, initialized = t[0], t[1], t[7]
        if not initialized or gross == 0:
            raise TickDataError(f"{pool} @ {block}: bitmap bit set for tick {tick} but ticks() says uninitialized")
        rows.append({"tick": tick, "liquidity_gross": str(gross), "liquidity_net": str(net)})
    return rows


def get_tick_snapshot(
    rpc: RpcClient,
    pool: str,
    block: int,
    words_each_side: int,
    concurrency: int = 4,
    root: Path = TICKS_ROOT,
) -> PoolSnapshot:
    """Snapshot of `pool` after `block`, with the tickBitmap words
    [w0 - words_each_side, w0 + words_each_side], w0 = the word holding the current tick.
    Reads the cache first and fetches only what is missing."""
    pool = to_checksum_address(pool)
    base = root / pool.lower() / f"{block:012d}"
    state_path = base / "state.parquet"
    if state_path.exists():
        state = pl.read_parquet(state_path).row(0, named=True)
    else:
        state = _fetch_state(rpc, pool, block)
        _atomic_write(pl.DataFrame([state], schema=STATE_SCHEMA), state_path)

    spacing = state["tick_spacing"]
    w0, _ = position(compress(state["tick"], spacing))
    word_lo, word_hi = w0 - words_each_side, w0 + words_each_side

    missing = [w for w in range(word_lo, word_hi + 1) if not _word_path(base, w).exists()]
    if missing:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            for w, rows in zip(missing, ex.map(lambda w: _fetch_word(rpc, pool, block, w, spacing), missing)):
                _atomic_write(pl.DataFrame(rows, schema=WORD_SCHEMA), _word_path(base, w))

    bitmap: dict[int, int] = {}
    liquidity_net: dict[int, int] = {}
    for w in range(word_lo, word_hi + 1):
        for row in pl.read_parquet(_word_path(base, w)).iter_rows(named=True):
            t = row["tick"]
            if t % spacing != 0:
                raise TickDataError(f"cached tick {t} is not a multiple of tickSpacing {spacing}")
            wp, bp = position(compress(t, spacing))
            if wp != w:
                raise TickDataError(f"cached tick {t} belongs to word {wp}, found in word file {w}")
            bitmap[w] = bitmap.get(w, 0) | (1 << bp)
            liquidity_net[t] = int(row["liquidity_net"])

    return PoolSnapshot(
        sqrt_price_x96=int(state["sqrt_price_x96"]),
        tick=state["tick"],
        liquidity=int(state["liquidity"]),
        fee=state["fee"],
        tick_spacing=spacing,
        bitmap=bitmap,
        liquidity_net=liquidity_net,
        word_lo=word_lo,
        word_hi=word_hi,
    )


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    pool, block = argv[0], int(argv[1])
    words = int(argv[2]) if len(argv) == 3 else 2
    url = load_rpc_url()
    rpc = RpcClient(url)
    try:
        snap = get_tick_snapshot(rpc, pool, block, words)
    except Exception as e:  # never let the RPC URL leak through an error message
        print(f"ticks failed: {redact(e, url)}", file=sys.stderr)
        return 1
    print(
        f"{pool} @ {block}: tick={snap.tick} liquidity={snap.liquidity} fee={snap.fee} "
        f"spacing={snap.tick_spacing} words=[{snap.word_lo},{snap.word_hi}] "
        f"initialized_ticks={len(snap.liquidity_net)} rpc_calls={rpc.calls}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
