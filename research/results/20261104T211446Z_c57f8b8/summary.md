# Replay 20261104T211446Z_c57f8b8

**Headline.** Over 1.5 hours (20,000 blocks, 1,140 swaps), the WETH/USDC pools showed 9 episodes with a gap above the fee threshold, 56% of closed episodes lasted one block, and their exact multi-tick gross profit sums to $0.0233; none is profitable after gas (mean $0.013 per arbitrage).

| Metric | Value |
|---|---|
| Blocks (N) | 20,000 (520861829..520881828) |
| Hours (H) | 1.53 |
| Swaps (S) | 1,140 |
| Gross episodes (G): gap > sum of fees | 9 |
| Net-positive episodes (M) | 0 |
| Closed within 1 block (P) | 55.6% of closed episodes |
| Median / p90 duration (blocks) | 1 / 10 |
| Median / p90 duration (seconds) | 0 / 2 |
| Episodes touching window edge (censored) | 0 |
| Total gross profit, exact multi-tick (USD) | $0.0233 |
| Total gross profit, single-tick approximation (USD), for comparison | $0.0234 |
| Total net profit, net-positive episodes only (USD) | $0.0000 |
| Single-tick peak trade stays before the next tickSpacing boundary (single-tick math provably exact; crossing a boundary does not mean L changes) | 22.2% of episodes |

## Per pair

| Pair (fee tiers) | Threshold (bps) | Episodes | Net-positive | Max gap (bps) | Median duration (blocks) | Gross, multi-tick (USD) | Gross, single-tick (USD) | Mean gas (USD) |
|---|---|---|---|---|---|---|---|---|
| 100-500 | 6 | 9 | 0 | 10.0 | 1 | $0.0233 | $0.0234 | $0.0134 |

## Single-tick vs exact multi-tick

Summed over the 9 episodes, the single-tick approximation gave **$0.023422** gross; the exact multi-tick simulation at the same peak blocks gives **$0.023342**. The single-tick number overstated gross profit by **$0.000080** (0.34% of the exact value).

- Stale liquidity: $0.000000. The single-tick formula re-run with the on-chain liquidity at the peak block gives $0.023422 (1 of 9 episodes had in-range liquidity changed by a Mint/Burn since the pool's last swap).
- Tick crossings: $0.000074, from the 1 episode(s) whose exact optimal trade crosses at least one initialized tick.
- Integer rounding: $0.000006 over the other 8 episodes (the exact profit is a whole number of raw USDC units, 1e-6 USDC; the single-tick formula is continuous).

| Pair | Peak block | Gap (bps) | Single-tick size / gross (USD) | Single-tick, on-chain L, gross (USD) | Multi-tick size / gross (USD) | Ticks crossed (cheap, exp.) | L matches replay |
|---|---|---|---|---|---|---|---|
| 100-500 | 520864964 | 6.07 | 0.91 / 0.000003 | 0.000003 | 1.22 / 0.000002 | 0, 0 | yes |
| 100-500 | 520871434 | 7.29 | 18.49 / 0.001193 | 0.001193 | 18.68 / 0.001192 | 0, 0 | yes |
| 100-500 | 520871441 | 7.28 | 18.26 / 0.001162 | 0.001162 | 18.31 / 0.001161 | 0, 0 | yes |
| 100-500 | 520871445 | 9.22 | 46.91 / 0.007551 | 0.007551 | 46.62 / 0.007477 | 1, 0 | yes |
| 100-500 | 520871518 | 6.70 | 10.05 / 0.000352 | 0.000352 | 10.14 / 0.000351 | 0, 0 | yes |
| 100-500 | 520871527 | 9.97 | 56.95 / 0.011280 | 0.011280 | 57.47 / 0.011279 | 0, 0 | yes |
| 100-500 | 520872986 | 7.17 | 16.79 / 0.000979 | 0.000979 | 16.78 / 0.000979 | 0, 0 | yes |
| 100-500 | 520873453 | 6.79 | 11.28 / 0.000442 | 0.000442 | 11.50 / 0.000442 | 0, 0 | no |
| 100-500 | 520874791 | 6.80 | 11.50 / 0.000460 | 0.000460 | 11.91 / 0.000459 | 0, 0 | yes |

Sizes differ more than profits because profit is flat near the optimum.

## Definitions

- An **episode** is a run of consecutive blocks in which the same pool pair's end-of-block gap exceeds the sum of the two pool fees. Duration in blocks = end - start + 1; in seconds = timestamp of the block that closed it minus the start block's timestamp (Arbitrum timestamps have 1 s resolution, so 1-block episodes often show 0 s). Durations exclude episodes still open at the window end.
- **Gross** is taken at the episode's peak block; **net** = gross - gas at that block.
- Totals sum one capture per episode.

## Gas assumption

- Method: **node_interface**. NodeInterface.gasEstimateComponents on a SwapRouter02 two-swap round trip (0.1 WETH, state-overridden sender) at block 520881828; router overhead likely overstates a direct-pool arb contract's gas.
- 100-500: 290,762 gas units (L1 component: 0)
- 100-3000: 290,750 gas units (L1 component: 0)
- 500-3000: 251,460 gas units (L1 component: 0)

## Simplifications (read before using these numbers)

- Gross profit is the EXACT multi-tick optimum at the episode's peak block: both legs run through the Uniswap v3 swap port (data/pricing/v3_swap.py, QuoterV2-parity tested) on the on-chain tick state at that block; optimal size by integer search (within a few raw units of the true optimum). It still assumes perfect execution at the end-of-block state and no competition.
- The peak block is chosen by the SINGLE-TICK gross (constant product on the current tick's virtual reserves), as in the first run; the single-tick numbers are kept as *_single_tick comparison columns.
- Gap detection uses in-range liquidity forward-filled from Swap events (Mint/Burn between swaps are not replayed); the multi-tick sizing reads the real on-chain liquidity at the peak block instead.
- Gap threshold is the sum of the two fees (spec); the exact break-even 1/((1-fa)(1-fb))-1 is slightly higher and is what the profit formula uses, so a few gross episodes can have ~0 profit.
- States are end-of-block; intra-block gaps that were opened and closed within one block are invisible.
- Pairs are evaluated independently; simultaneous episodes on pairs sharing a pool compete for the same liquidity, so summed profits overstate what one arbitrageur could capture.
- Gas: one representative estimate per pair (see gas_assumption), priced at each episode's peak-block base fee.
- Profits in USDC with 1 USDC = 1 USD.

## Reproducibility

- Git commit `c57f8b85d33d8a9adf9a2368714e6d72b349b6d5`; RPC provider `arb-mainnet.g.alchemy.com`; registry verified at block 520884405.
- Full config in `config.json`; episodes in `episodes.parquet`.
