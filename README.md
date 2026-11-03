# Arbitrum DEX Arbitrage Research & Execution

Research into atomic arbitrage between Uniswap v3 WETH/USDC pools on Arbitrum One.
Data only so far: no trading, no keys.

## Setup

```bash
uv sync
cp .env.example .env   # then set ARBITRUM_RPC_URL (archive-capable, e.g. Alchemy)
```

## Pipeline

```bash
uv run python -m data.pools.registry     # verify pools on-chain -> configs/pools.yaml
uv run python -m data.collectors.swaps   # Swap logs -> data/cache/swaps/ (Parquet, cached)
uv run pytest                            # unit tests + on-chain checks (need .env and cache)
```

## Simplifications and assumptions

- **Window = last 20,000 *finalized* blocks**, not `latest`, so cached data is never reorged.
- **Prices are mid (spot) prices** from `sqrtPriceX96`: no fee, no price impact. A Swap's
  `sqrtPriceX96` is the price *after* that swap.
- **Big integers** (amounts, sqrtPriceX96, liquidity) are stored as exact decimal strings.
  The `usdc_per_weth` float column is rounded to float64.
- **Only Uniswap v3, fee tiers 100/500/3000.** Other tiers and forks are not included yet.

See `LEARNING.md` for the reasoning behind each component.
