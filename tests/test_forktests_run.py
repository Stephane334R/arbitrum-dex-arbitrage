"""Unit tests for the forge-output parsing in research/forktests/run.py (no forge, no RPC)."""

import pytest

from research.forktests.run import (
    EXECUTE_SIG,
    EXECUTOR,
    fork_config,
    flatten_tests,
    function_gas,
    render_summary,
    synthetic_profit_usdc,
)

FORGE_JSON = {
    "test/V3ArbExecutor.t.sol:V3ArbExecutorTest": {
        "duration": "1s",
        "warnings": [],
        "test_results": {
            "test_SYNTHETIC_gapAfterLargeSwap_executeProfits()": {
                "status": "Success",
                "reason": None,
                "fork_block_number": 520884405,
                "counterexample": None,
                "decoded_logs": ["SYNTHETIC profit (USDC): 519.940023"],
                "kind": {"Unit": {"gas": 885004}},
                "traces": ["dropped"],
            },
            "testFuzz_neverLosesWithoutReverting(uint256,bool,uint256)": {
                "status": "Success",
                "reason": None,
                "fork_block_number": 520884405,
                "counterexample": None,
                "decoded_logs": [],
                "kind": {"Fuzz": {"runs": 256, "mean_gas": 1149316, "median_gas": 1277137}},
            },
        },
    }
}

GAS = [
    {
        "contract": EXECUTOR,
        "deployment": {"gas": 1, "size": 1},
        "functions": {EXECUTE_SIG: {"calls": 1, "min": 340692, "mean": 340692, "median": 340692, "max": 340692}},
    }
]


def test_flatten_tests_drops_traces_and_reads_kinds():
    rows = flatten_tests(FORGE_JSON)
    # Sorted by test name, ASCII order: "testFuzz..." < "test_SYNTHETIC..." ('F' < '_').
    assert [r["kind"] for r in rows] == ["fuzz", "unit"]
    unit = next(r for r in rows if r["kind"] == "unit")
    fuzz = next(r for r in rows if r["kind"] == "fuzz")
    assert unit["gas"] == 885004 and "traces" not in unit
    assert (fuzz["runs"], fuzz["median_gas"]) == (256, 1277137)


def test_synthetic_profit_is_exact_string():
    assert synthetic_profit_usdc(flatten_tests(FORGE_JSON)) == "519.940023"


def test_synthetic_profit_missing_log_fails_loudly():
    data = {"s": {"test_results": {"test_SYNTHETIC_gapAfterLargeSwap_executeProfits()": {
        "status": "Success", "kind": {"Unit": {"gas": 1}}, "decoded_logs": []}}}}
    with pytest.raises(ValueError, match="no 'SYNTHETIC profit"):
        synthetic_profit_usdc(flatten_tests(data))


def test_function_gas_and_missing_contract():
    assert function_gas(GAS, EXECUTOR, EXECUTE_SIG)["max"] == 340692
    with pytest.raises(KeyError):
        function_gas(GAS, "src/Other.sol:Other", EXECUTE_SIG)


def test_fork_config_reads_pinned_block():
    cfg = fork_config({"profile": {"default": {"fork_block_number": 520884405, "eth_rpc_url": "arbitrum"}},
                       "fuzz": {"runs": 256, "seed": "0x2a"}})
    assert cfg["fork_block_number"] == 520884405 and cfg["fuzz"]["seed"] == "0x2a"


def test_render_summary_labels_synthetic_and_l2_gas():
    rows = flatten_tests(FORGE_JSON)
    g = GAS[0]["functions"][EXECUTE_SIG]
    meta = {
        "run_id": "X", "fork": {"fork_block_number": 520884405, "solc_version": "0.8.28", "fuzz": {"seed": "0x2a"}},
        "synthetic_profit_usdc": "519.940023", "gas": {"synthetic_execute": g, "execute_all_calls": g},
        "git_commit": "abc", "git_dirty": False, "toolchain": {"forge": "forge 1.9.0", "forge_std_commit": "f3d"},
        "rpc_provider": "example.org",
    }
    md = render_summary(meta, rows)
    assert "2/2 tests pass" in md and "**519.940023 USDC**" in md and "340,692" in md
    assert "SYNTHETIC" in md and "L1 data fee is not included" in md
    assert "uncommitted" not in md
