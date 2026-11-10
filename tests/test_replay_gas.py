"""Tests for research/replay/gas.py: execute() gas from fork-test results, per-episode
intrinsic + L1 gas (fake NodeInterface), cache reuse, and the gas sensitivity table. No RPC."""

import json

import polars as pl
import pytest
from eth_abi import decode, encode

from research.replay import gas
from research.replay.replay import PoolMeta

UNI100, UNI500 = "0x6f38e884725a116C9C7fBF208e79FE8828a2595F", "0xC6962004f452bE9203591991D15f6b388e09E8D0"
METAS = [PoolMeta(UNI100, 100, 1, True, 18, 6, "100"), PoolMeta(UNI500, 500, 10, True, 18, 6, "500")]


def _forktest_dir(tmp_path, calls=1, gas_value=340692):
    run = tmp_path / "ft"
    run.mkdir()
    cfg = {"gas": {"synthetic_execute": {"calls": calls, "min": gas_value, "mean": gas_value, "median": gas_value,
                                         "max": gas_value},
                   "execute_all_calls": {"calls": 269, "min": 22839, "mean": 621504, "median": 704361, "max": 963430}},
           "synthetic_scenario": {"execute": "poolA=0.30%, poolB=0.05%"}, "fork": {"fork_block_number": 1},
           "git_commit": "abc1234def"}
    (run / "config.json").write_text(json.dumps(cfg))
    return tmp_path


def test_executor_gas_reads_the_single_synthetic_execute(tmp_path):
    g = gas.executor_gas("ft", _forktest_dir(tmp_path))
    assert g["execute_gas_l2"] == 340692 and g["all_execute_calls"]["median"] == 704361


def test_executor_gas_rejects_aggregated_calls(tmp_path):
    with pytest.raises(ValueError, match="exactly one"):
        gas.executor_gas("ft", _forktest_dir(tmp_path, calls=3))


def test_execute_calldata_matches_signature():
    cd = gas.execute_calldata(UNI100, UNI500, False, 25_000_000, 1)
    raw = bytes.fromhex(cd[2:])
    from eth_utils import keccak
    assert raw[:4] == keccak(text="execute(address,address,bool,uint256,uint256)")[:4]  # V3ArbExecutor.execute
    a, b, z, amt, mp = decode(["address", "address", "bool", "uint256", "uint256"], raw[4:])
    assert (a.lower(), b.lower(), z, amt, mp) == (UNI100.lower(), UNI500.lower(), False, 25_000_000, 1)


class FakeNode:
    """NodeInterface / ArbGasInfo stand-in: L1 gas grows with the block number."""

    def __init__(self):
        self.calls = 0

    def call(self, method, params):
        self.calls += 1
        if method == "eth_getCode":
            return "0x"
        to, data, block = params[0]["to"].lower(), params[0]["data"], int(params[1], 16)
        sel = bytes.fromhex(data[2:10])
        if to == gas.NODE_INTERFACE.lower():
            assert sel == gas._sel("gasEstimateComponents(address,bool,bytes)")
            l1 = block - 1000
            return "0x" + encode(["uint64", "uint64", "uint256", "uint256"], [22579 + l1, l1, 20_000_000, 7 * l1]).hex()
        if sel == gas._sel("getL1BaseFeeEstimate()"):
            return "0x" + encode(["uint256"], [7 * (block - 1000)]).hex()
        if sel == gas._sel("getPricesInWei()"):
            return "0x" + encode(["uint256"] * 6, [0, 16 * 7 * (block - 1000), 0, 0, 0, 0]).hex()
        raise AssertionError(f"unexpected call to {to}")


def _episodes():
    return pl.DataFrame({
        "pair": ["100-500", "100-500"], "peak_block": [1000, 1005], "cheap_pool": [UNI100, UNI500],
        "expensive_pool": [UNI500, UNI100], "size_usdc": [25.0, 10.0], "peak_gross_usdc": [0.05, 0.002],
        "peak_gross_usdc_single_tick": [0.051, 0.002], "price_cheap": [3000.0, 3000.0], "price_expensive": [3003.0, 3003.0],
    })


def test_episode_gas_sums_forge_intrinsic_and_l1_at_the_peak_block(tmp_path, monkeypatch):
    monkeypatch.setattr(gas, "CACHE_PATH", tmp_path / "l1.parquet")
    node = FakeNode()
    out = gas.episode_gas(node, _episodes(), METAS, {"execute_gas_l2": 340692}, 1.0, {1000: 20_000_000, 1005: 30_000_000})
    assert out["gas_units_intrinsic_l2"].to_list() == [22579, 22579]
    assert out["gas_units_l1"].to_list() == [0, 5]  # measured per block
    assert out["gas_units"].to_list() == [340692 + 22579, 340692 + 22579 + 5]
    assert out["peak_gas_usdc"][1] == pytest.approx((340692 + 22579 + 5) * 30_000_000 / 1e18 * 3001.5)
    assert out["peak_net_usdc"][0] == pytest.approx(0.05 - out["peak_gas_usdc"][0])
    assert out["wei_per_l1_calldata_byte"].to_list() == [0, 16 * 7 * 5]
    # second call: everything comes from the Parquet cache
    n = node.calls
    gas.episode_gas(node, _episodes(), METAS, {"execute_gas_l2": 340692}, 1.0, {1000: 20_000_000, 1005: 30_000_000})
    assert node.calls == n


def test_sensitivity_counts_m_at_each_multiplier():
    e = pl.DataFrame({"peak_gross_usdc": [0.03, 0.012, 0.006], "peak_gas_usdc": [0.01, 0.01, 0.01]})
    s = gas.sensitivity(e)
    assert [r["M"] for r in s] == [3, 2, 1]  # x0.5: all; x1: 0.03, 0.012; x2: 0.03
    assert s[1]["net_usdc"] == pytest.approx(0.02 + 0.002)
    assert gas.sensitivity(pl.DataFrame())[0] == {"k": 0.5, "M": 0, "net_usdc": 0.0}
