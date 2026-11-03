"""Pool registry: verify WETH/USDC Uniswap v3 pools on Arbitrum One on-chain and save them.

Nothing here is trusted from memory. The addresses below are *inputs to verify*:
- the factory must have code and must recognise each pool (pool.factory() == factory);
- each token's symbol() and decimals() are read from the chain and must match expectations;
- each pool comes from factory.getPool(), and its token0/token1/fee/tickSpacing are read back
  and checked against the factory's feeAmountTickSpacing().

All reads are pinned to one block number, so the registry is a consistent snapshot.

Usage:  uv run python -m data.pools.registry
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from web3 import Web3

from data.abi import ERC20, UNISWAP_V3_FACTORY, UNISWAP_V3_POOL
from data.rpc import ARBITRUM_CHAIN_ID, get_web3, load_rpc_url, provider_name, redact

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
REGISTRY_PATH = Path(__file__).resolve().parents[2] / "configs" / "pools.yaml"

# Inputs to verify (from the task spec, not trusted).
FACTORY = "0x1F98431c8aD98523631AE4a59f267346ea31F984"
TOKENS = {
    "WETH": {"address": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", "expected_decimals": 18},
    "USDC": {"address": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "expected_decimals": 6},
}
FEE_TIERS = [100, 500, 3000]


class VerificationError(Exception):
    pass


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise VerificationError(msg)


def verify_token(w3: Web3, address: str, expected_symbol: str, expected_decimals: int, block: int) -> dict:
    addr = Web3.to_checksum_address(address)
    _require(len(w3.eth.get_code(addr, block_identifier=block)) > 0, f"{expected_symbol}: no code at {addr}")
    c = w3.eth.contract(address=addr, abi=ERC20)
    symbol = c.functions.symbol().call(block_identifier=block)
    name = c.functions.name().call(block_identifier=block)
    decimals = c.functions.decimals().call(block_identifier=block)
    _require(symbol == expected_symbol, f"{addr}: symbol() is {symbol!r}, expected {expected_symbol!r}")
    _require(decimals == expected_decimals, f"{addr}: decimals() is {decimals}, expected {expected_decimals}")
    return {"address": addr, "symbol": symbol, "name": name, "decimals": decimals}


def verify_pool(w3: Web3, factory: str, token_a: dict, token_b: dict, fee: int, block: int) -> dict | None:
    """Return the verified pool, or None if the factory has no pool for this fee tier."""
    f = w3.eth.contract(address=factory, abi=UNISWAP_V3_FACTORY)
    pool_addr = f.functions.getPool(token_a["address"], token_b["address"], fee).call(block_identifier=block)
    if pool_addr == ZERO_ADDRESS:
        return None
    pool_addr = Web3.to_checksum_address(pool_addr)
    _require(len(w3.eth.get_code(pool_addr, block_identifier=block)) > 0, f"pool {pool_addr}: no code")

    p = w3.eth.contract(address=pool_addr, abi=UNISWAP_V3_POOL)
    t0 = p.functions.token0().call(block_identifier=block)
    t1 = p.functions.token1().call(block_identifier=block)
    pool_fee = p.functions.fee().call(block_identifier=block)
    spacing = p.functions.tickSpacing().call(block_identifier=block)
    pool_factory = p.functions.factory().call(block_identifier=block)
    factory_spacing = f.functions.feeAmountTickSpacing(fee).call(block_identifier=block)

    by_addr = {token_a["address"]: token_a, token_b["address"]: token_b}
    _require(pool_factory == factory, f"pool {pool_addr}: factory() is {pool_factory}, expected {factory}")
    _require({t0, t1} == set(by_addr), f"pool {pool_addr}: tokens {t0},{t1} do not match")
    _require(int(t0, 16) < int(t1, 16), f"pool {pool_addr}: token0 is not the lower address")
    _require(pool_fee == fee, f"pool {pool_addr}: fee() is {pool_fee}, expected {fee}")
    _require(spacing == factory_spacing, f"pool {pool_addr}: tickSpacing {spacing} != factory's {factory_spacing}")

    def tok(a: str) -> dict:
        return {"address": a, "symbol": by_addr[a]["symbol"], "decimals": by_addr[a]["decimals"]}

    return {
        "address": pool_addr,
        "dex": "uniswap_v3",
        "fee": pool_fee,
        "tick_spacing": spacing,
        "token0": tok(t0),
        "token1": tok(t1),
    }


def build_registry(w3: Web3, rpc_provider: str) -> dict:
    block = w3.eth.block_number
    factory = Web3.to_checksum_address(FACTORY)
    _require(len(w3.eth.get_code(factory, block_identifier=block)) > 0, f"factory: no code at {factory}")

    tokens = {
        sym: verify_token(w3, t["address"], sym, t["expected_decimals"], block) for sym, t in TOKENS.items()
    }
    pools, skipped = [], []
    for fee in FEE_TIERS:
        pool = verify_pool(w3, factory, tokens["WETH"], tokens["USDC"], fee, block)
        if pool is None:
            skipped.append(fee)
        else:
            pools.append(pool)

    return {
        "chain_id": ARBITRUM_CHAIN_ID,
        "verified_at_block": block,
        "rpc_provider": rpc_provider,
        "factory": {"address": factory, "dex": "uniswap_v3"},
        "tokens": tokens,
        "pools": pools,
        "skipped_fee_tiers": skipped,
    }


def save_registry(registry: dict, path: Path = REGISTRY_PATH) -> None:
    header = "# Generated by data/pools/registry.py from on-chain reads. Do not edit by hand.\n"
    path.write_text(header + yaml.safe_dump(registry, sort_keys=False))


def load_registry(path: Path = REGISTRY_PATH) -> dict:
    return yaml.safe_load(path.read_text())


def main() -> int:
    url = load_rpc_url()
    try:
        registry = build_registry(get_web3(url), provider_name(url))
    except Exception as e:  # never let the RPC URL leak through an error message
        print(f"registry failed: {redact(e, url)}", file=sys.stderr)
        return 1
    save_registry(registry)
    print(f"verified at block {registry['verified_at_block']}")
    for sym, t in registry["tokens"].items():
        print(f"  {sym}: {t['address']} symbol={t['symbol']} name={t['name']!r} decimals={t['decimals']}")
    for p in registry["pools"]:
        print(
            f"  pool fee={p['fee']} tickSpacing={p['tick_spacing']} {p['address']} "
            f"token0={p['token0']['symbol']} token1={p['token1']['symbol']}"
        )
    if registry["skipped_fee_tiers"]:
        print(f"  skipped fee tiers (no pool): {registry['skipped_fee_tiers']}")
    print(f"saved {REGISTRY_PATH.relative_to(Path.cwd()) if REGISTRY_PATH.is_relative_to(Path.cwd()) else REGISTRY_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
