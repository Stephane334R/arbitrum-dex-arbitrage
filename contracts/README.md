# contracts/

Foundry project: `V3ArbExecutor`, an atomic two-pool Uniswap v3 arbitrage executor, plus fork
tests on Arbitrum One. See `LEARNING.md` (section *Contracts*) for the callback flow and every
revert path.

## Setup

- Install Foundry (`foundryup`). Tested with forge 1.9.0; solc 0.8.28; forge-std v1.17.0 (git submodule).
- `git submodule update --init --recursive`
- The RPC URL is read from `ARBITRUM_RPC_URL` in the repo-root `.env` (git-ignored). Foundry only
  loads `.env` from this directory, so link it once: `ln -s ../.env contracts/.env` (also git-ignored).
  It needs an archive-capable endpoint: tests fork at a past block.

## Run

```bash
cd contracts
forge test -vv
forge test --gas-report
```

`foundry.toml` pins the fork (`eth_rpc_url = "arbitrum"`, `fork_block_number = 520884405`): every
test runs on that block, which is the same block as `configs/pools.yaml` and
`tests/vectors/v3_swaps.json`. Forge caches fork RPC responses under `~/.foundry/cache/rpc/`,
so the first run is slow and later runs take seconds.

## Tests

- `test/V3ArbExecutor.t.sol`: SYNTHETIC gap scenarios (a funded test account dumps WETH into the
  0.30% pool, so the gap is synthetic, not historical), callback authentication, every
  `execute` revert path, rescue, and a fuzz test that execute never ends in a loss without reverting.
- `test/V3SwapParity.t.sol`: real swaps on the forked pools match the Python port's vectors exactly.
- `test/utils/ForkBase.sol`: verified addresses (re-checked against the factory in `setUp`) and a
  test-only `PoolSwapper`.
