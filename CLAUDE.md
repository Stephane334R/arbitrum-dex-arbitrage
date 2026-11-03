# Arbitrum DEX Arbitrage Research & Execution

Research and execution system for atomic arbitrage between concentrated-liquidity
pools (Uniswap v3 family) on Arbitrum One (chain id 42161).

## Rules
- Honesty first: never present an approximation as exact. Label every simplification
  (single-tick sizing, fixed gas estimate, synthetic fork scenario) in code comments,
  README and results.
- Verify every on-chain address from the chain itself (factory.getPool, token
  symbol()/decimals(), pool fee()/tickSpacing()) before using it. Never trust an
  address from memory.
- Secrets: RPC URLs and keys live only in .env (git-ignored). Never print or commit them.
- Reproducibility: every result is saved with its config (block range, pool addresses,
  fee tiers, RPC provider name, git commit) in research/results/<run_id>/.
- Cache raw RPC data locally (Parquet) and never fetch the same block range twice.
- Small steps: one component per change, with tests. Run tests before declaring done.
- After each component, update LEARNING.md with five answers: what it does, why this
  implementation, assumptions, what can go wrong, how to make it production-grade.
- No real-money transactions. No private keys in this repo.

## Layout
- data/       collectors (RPC, logs), pool registry, pricing math
- research/   replay engine, analytics, notebooks, results/
- contracts/  Foundry project (src/, test/, script/)
- tests/      Python tests
- configs/    YAML configs (pools, block ranges)

## Stack
Python 3.11+, uv, web3.py, polars, pyarrow, pytest, matplotlib; Foundry for Solidity.

## Git
- No Claude/Anthropic attribution anywhere: no `Co-Authored-By: Claude` trailer, no
  "Generated with Claude Code" line in commits, PRs, repo descriptions or push metadata.
