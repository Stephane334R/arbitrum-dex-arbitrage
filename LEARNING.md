# Learning Log

For each component: what it does, why this implementation, assumptions,
what can go wrong, how to make it production-grade.

## Data Collectors

## Pool Registry

### `data/pools/registry.py` → `configs/pools.yaml`

**What it does.** Starting from the Uniswap v3 factory, WETH and USDC addresses given in the
spec, it verifies everything on-chain and writes a registry of WETH/USDC pools for fee tiers
100/500/3000. Tiers with no pool are listed in `skipped_fee_tiers`.

**Why this implementation.**
- Every input address is treated as unverified. The factory and each token must have
  contract code. Each token's `symbol()` and `decimals()` must match what we expect, and
  `name()` is recorded too (native USDC reports `USD Coin`). Pools come from
  `factory.getPool()`, never from memory.
- Each pool is cross-checked in both directions. `pool.factory()` must be the factory, the
  pool's token set must be {WETH, USDC}, `fee()` must equal the requested tier, and
  `tickSpacing()` must equal the factory's `feeAmountTickSpacing(fee)`.
- All reads are pinned to one block (`verified_at_block`), so the registry is a consistent
  snapshot. The RPC host name, never the key, is saved for reproducibility.
- The output is plain YAML, so it is easy to diff and review in git.

**Assumptions.**
- A token that has code and returns the expected symbol and decimals is the real token.
  A malicious contract could imitate this. The safeguard is that the addresses come from a
  trusted source *and* are checked. Checking alone does not authenticate.
- Fee tiers 100/500/3000 are enough. 10000 (1%) exists in Uniswap v3 but was not requested.
- Uniswap v3 only. Forks such as Sushi v3 and PancakeSwap v3 have their own factories.

**What can go wrong.**
- USDC is upgradeable (a proxy), so its behaviour can change after verification. The
  registry should be re-run periodically.
- Confusing native USDC with bridged USDC.e. Both have 6 decimals, so the symbol check is
  what separates them.
- The RPC endpoint could lie. Everything here trusts a single provider.

**Production-grade.** Cross-check reads against a second, independent RPC provider. Verify the
code hash of the pool against the factory's init code hash, i.e. recompute the CREATE2
address from (factory, token0, token1, fee). Include more fee tiers and forks. Run the
registry check in CI and alert on any change.

## Pricing Math

## Replay Engine

## Analytics

## Contracts
