"""Integration test: the price in the latest cached Swap log equals pool.slot0() at that block.

Uniswap v3 sqrtPriceX96 only changes in swap() (mint/burn/collect/flash leave it unchanged),
so slot0 read at the end of block N must equal the sqrtPriceX96 of the last Swap in block N.
The test therefore checks exact integer equality; the float price may differ only by rounding.
"""

import os

import pytest

from data.collectors.swaps import load_swaps
from data.pools.registry import load_registry
from data.pricing.v3_price import usdc_per_weth

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def w3():
    from dotenv import load_dotenv

    from data.rpc import get_web3

    load_dotenv()
    if not os.environ.get("ARBITRUM_RPC_URL"):
        pytest.skip("ARBITRUM_RPC_URL not set")
    return get_web3()


@pytest.mark.parametrize("fee", [100, 500, 3000])
def test_latest_swap_price_matches_slot0(w3, fee):
    from data.abi import UNISWAP_V3_POOL

    registry = load_registry()
    pool = next((p for p in registry["pools"] if p["fee"] == fee), None)
    if pool is None:
        pytest.skip(f"no verified pool for fee {fee}")
    swaps = load_swaps(pool["address"])
    if swaps.is_empty():
        pytest.skip("no cached swaps; run `uv run python -m data.collectors.swaps` first")

    last = swaps.row(-1, named=True)  # sorted by (block_number, log_index)
    contract = w3.eth.contract(address=pool["address"], abi=UNISWAP_V3_POOL)
    slot0 = contract.functions.slot0().call(block_identifier=last["block_number"])

    log_sqrt = int(last["sqrt_price_x96"])
    assert slot0[0] == log_sqrt
    assert slot0[1] == last["tick"]
    p_log, p_slot0 = usdc_per_weth(log_sqrt, pool, registry), usdc_per_weth(slot0[0], pool, registry)
    assert p_log == p_slot0
    assert 100 < float(p_log) < 100_000  # sanity: a plausible ETH price in USDC
