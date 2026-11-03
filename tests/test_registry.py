"""Offline consistency checks on the saved, on-chain-verified registry (configs/pools.yaml)."""

from data.pools.registry import load_registry

# Uniswap v3 fee -> tickSpacing as set by the factory (re-verified on-chain by the registry).
EXPECTED_SPACING = {100: 1, 500: 10, 3000: 60}


def test_registry_is_consistent():
    reg = load_registry()
    assert reg["chain_id"] == 42161
    tokens = {t["address"] for t in reg["tokens"].values()}
    assert reg["tokens"]["WETH"]["decimals"] == 18 and reg["tokens"]["USDC"]["decimals"] == 6
    fees = [p["fee"] for p in reg["pools"]] + reg["skipped_fee_tiers"]
    assert sorted(fees) == [100, 500, 3000]
    for p in reg["pools"]:
        assert {p["token0"]["address"], p["token1"]["address"]} == tokens
        assert int(p["token0"]["address"], 16) < int(p["token1"]["address"], 16)
        assert p["tick_spacing"] == EXPECTED_SPACING[p["fee"]]
    assert len({p["address"] for p in reg["pools"]}) == len(reg["pools"])
