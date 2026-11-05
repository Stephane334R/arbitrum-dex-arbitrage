"""Run the Foundry fork tests and save the results with their config.

    uv run python -m research.forktests.run

Writes research/results/<run_id>/ (run_id = UTC time + short git commit):
  config.json       fork block, pools, toolchain, fuzz settings, git commit, RPC provider name
  tests.json        one row per test: status, kind, gas / fuzz stats, decoded logs
  gas_report.json   `forge test --gas-report --json` over the whole suite
  summary.md        headline numbers, every one read from forge's own output

Three forge invocations (all on the fork pinned in contracts/foundry.toml):
  1. `forge test --json -vv`: statuses and logs (the SYNTHETIC profit is a test log line).
  2. `forge test --gas-report --json`: gas over every call made by the suite.
  3. same, restricted to the SYNTHETIC success test: gas of that one execute() call.

The RPC URL is never written anywhere: only its host name (data.rpc.provider_name).
Exit code 1 if any test fails (results are still saved, marked as failing).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from data.rpc import load_rpc_url, provider_name, redact
from research.replay.run import git_info

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"
RESULTS_ROOT = ROOT / "research" / "results"

EXECUTOR = "src/V3ArbExecutor.sol:V3ArbExecutor"
EXECUTE_SIG = "execute(address,address,bool,uint256,uint256)"
SYNTHETIC_TEST = "test_SYNTHETIC_gapAfterLargeSwap_executeProfits"
PROFIT_RE = re.compile(r"^SYNTHETIC profit \(USDC\): (\d+(?:\.\d+)?)$")


# ------------------------------------------------------------------ pure parsing


def flatten_tests(forge_json: dict) -> list[dict]:
    """`forge test --json` output -> one dict per test, without traces."""
    rows = []
    for suite, body in sorted(forge_json.items()):
        for name, r in sorted(body["test_results"].items()):
            kind_name, kind = next(iter(r["kind"].items()))
            row = {
                "suite": suite,
                "test": name,
                "status": r["status"],
                "reason": r.get("reason"),
                "kind": kind_name.lower(),
                "fork_block_number": r.get("fork_block_number"),
                "decoded_logs": r.get("decoded_logs") or [],
            }
            if kind_name == "Unit":
                row["gas"] = kind["gas"]
            elif kind_name == "Fuzz":
                row.update(runs=kind["runs"], mean_gas=kind["mean_gas"], median_gas=kind["median_gas"])
                row["counterexample"] = r.get("counterexample")
            rows.append(row)
    return rows


def synthetic_profit_usdc(rows: list[dict]) -> str:
    """The SYNTHETIC test logs its profit with 6 decimals; keep it as an exact decimal string."""
    for row in rows:
        if row["test"].startswith(SYNTHETIC_TEST + "("):
            for line in row["decoded_logs"]:
                m = PROFIT_RE.match(line.strip())
                if m:
                    return m.group(1)
            raise ValueError(f"{SYNTHETIC_TEST} has no 'SYNTHETIC profit (USDC): ...' log line")
    raise ValueError(f"{SYNTHETIC_TEST} not found in forge output")


def function_gas(gas_report: list[dict], contract: str, signature: str) -> dict:
    """{calls, min, mean, median, max} for one function in `forge test --gas-report --json`."""
    for c in gas_report:
        if c["contract"] == contract:
            return c["functions"][signature]
    raise KeyError(f"{contract} not in gas report")


def fork_config(foundry_toml: dict) -> dict:
    p = foundry_toml["profile"]["default"]
    return {
        "fork_block_number": p["fork_block_number"],
        "rpc_alias": p["eth_rpc_url"],
        "solc_version": p.get("solc_version"),
        "evm_version": p.get("evm_version"),
        "optimizer_runs": p.get("optimizer_runs"),
        "fuzz": foundry_toml.get("fuzz", {}),
    }


def render_summary(meta: dict, rows: list[dict]) -> str:
    passed = sum(r["status"] == "Success" for r in rows)
    g_syn, g_all = meta["gas"]["synthetic_execute"], meta["gas"]["execute_all_calls"]
    fuzz = [r for r in rows if r["kind"] == "fuzz"]
    lines = [
        f"# Foundry fork tests {meta['run_id']}",
        "",
        f"**Result.** {passed}/{len(rows)} tests pass on an Arbitrum One fork at block "
        f"{meta['fork']['fork_block_number']}. SYNTHETIC scenario: a test account dumps 100 WETH into "
        f"the 0.30% pool, then `execute` (0.30% -> 0.05%, 50,000 USDC in) makes "
        f"**{meta['synthetic_profit_usdc']} USDC** gross, using {g_syn['max']:,} L2 gas.",
        "",
        "The gap is SYNTHETIC: it does not exist at the pinned block (the same trade without the dump "
        "reverts with `CannotRepay`). The profit shows the contract works; it is not an estimate of "
        "real opportunity. Gas is forge's L2 execution gas; Arbitrum's L1 data fee is not included.",
        "",
        "| Suite | Test | Status | Gas |",
        "|---|---|---|---:|",
    ]
    for r in rows:
        if r["kind"] == "fuzz":
            gas = f"{r['runs']} runs, median {r['median_gas']:,}"
        else:
            gas = f"{r['gas']:,}"
        lines.append(f"| {r['suite'].split(':')[-1]} | `{r['test']}` | {r['status']} | {gas} |")
    lines += [
        "",
        "Test gas includes setup inside the test (e.g. the SYNTHETIC dump), not only `execute`.",
        "",
        "## `execute` gas",
        "",
        f"- SYNTHETIC success, alone: {g_syn['max']:,}.",
        f"- All {g_all['calls']} calls in the suite (reverts and large fuzz sizes included): "
        f"min {g_all['min']:,}, median {g_all['median']:,}, max {g_all['max']:,}.",
        "",
        "## Reproduce",
        "",
        f"- git commit `{meta['git_commit']}`{' (with uncommitted changes)' if meta['git_dirty'] else ''}; "
        f"{meta['toolchain']['forge']}; solc {meta['fork']['solc_version']}; "
        f"forge-std `{meta['toolchain']['forge_std_commit']}`; RPC provider {meta['rpc_provider']}.",
        f"- Fuzz: {fuzz[0]['runs'] if fuzz else 0} runs, seed `{meta['fork']['fuzz'].get('seed')}` "
        "(from contracts/foundry.toml).",
        "- `uv run python -m research.forktests.run` (or `cd contracts && forge test -vv`).",
    ]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ forge


def _forge() -> str:
    exe = shutil.which("forge") or str(Path.home() / ".foundry" / "bin" / "forge")
    if not Path(exe).exists():
        raise RuntimeError("forge not found (install Foundry with foundryup)")
    return exe


def _forge_json(args: list[str], env: dict, url: str):
    p = subprocess.run([_forge(), "test", *args], cwd=CONTRACTS, env=env, capture_output=True, text=True)
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"forge {' '.join(args)} gave no JSON:\n{redact(p.stderr[-2000:], url)}") from e


def run() -> int:
    url = load_rpc_url()
    env = {**os.environ, "ARBITRUM_RPC_URL": url}
    git = git_info()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + (git["commit"][:7] or "nogit")

    rows = flatten_tests(_forge_json(["--json", "-vv"], env, url))
    gas_all = _forge_json(["--gas-report", "--json"], env, url)
    gas_syn = _forge_json(["--gas-report", "--json", "--match-test", SYNTHETIC_TEST], env, url)

    foundry_toml = tomllib.loads((CONTRACTS / "foundry.toml").read_text())
    forge_version = subprocess.run([_forge(), "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    forge_std = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=CONTRACTS / "lib" / "forge-std", capture_output=True, text=True
    ).stdout.strip()

    meta = {
        "description": "Foundry fork tests of contracts/src/V3ArbExecutor.sol and on-chain swap parity",
        "run_id": run_id,
        "chain_id": 42161,
        "fork": fork_config(foundry_toml),
        "state_semantics": "fork state after `fork_block_number`",
        "pools": {
            "factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
            "poolA (0.30%)": "0xc473e2aEE3441BF9240Be85eb122aBB059A3B57c",
            "poolB (0.05%)": "0xC6962004f452bE9203591991D15f6b388e09E8D0",
            "also used by parity (0.01%)": "0x6f38e884725a116C9C7fBF208e79FE8828a2595F",
            "source": "configs/pools.yaml; re-checked with factory.getPool in contracts/test/utils/ForkBase.sol setUp",
        },
        "synthetic_scenario": {
            "label": "SYNTHETIC",
            "dump": "100 WETH sold into the 0.30% pool by a deal()-funded test account",
            "execute": "poolA=0.30%, poolB=0.05%, zeroForOne=false (USDC in), amountIn=50,000 USDC, minProfit=1 USDC",
        },
        "synthetic_profit_usdc": synthetic_profit_usdc(rows),
        "gas": {
            "unit": "L2 execution gas as measured by forge; excludes Arbitrum L1 data fee",
            "synthetic_execute": function_gas(gas_syn, EXECUTOR, EXECUTE_SIG),
            "execute_all_calls": function_gas(gas_all, EXECUTOR, EXECUTE_SIG),
        },
        "tests_passed": sum(r["status"] == "Success" for r in rows),
        "tests_total": len(rows),
        "vectors_file": "tests/vectors/v3_swaps.json",
        "toolchain": {"forge": forge_version, "forge_std_commit": forge_std},
        "rpc_provider": provider_name(url),
        "git_commit": git["commit"],
        "git_dirty": git["dirty"],
    }

    out = RESULTS_ROOT / run_id
    out.mkdir(parents=True)
    (out / "config.json").write_text(json.dumps(meta, indent=2) + "\n")
    (out / "tests.json").write_text(json.dumps(rows, indent=2) + "\n")
    (out / "gas_report.json").write_text(json.dumps(gas_all, indent=2) + "\n")
    (out / "summary.md").write_text(render_summary(meta, rows))
    print(f"saved {out.relative_to(ROOT)}: {meta['tests_passed']}/{meta['tests_total']} pass")
    return 0 if meta["tests_passed"] == meta["tests_total"] else 1


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
