# Replay 20261103T134604Z_5a6404c

**Headline.** Over 1.5 hours (20,000 blocks, 1,140 swaps), the WETH/USDC pools showed 9 episodes with a gap above the fee threshold, 56% of closed episodes lasted one block, and their single-tick gross upper bound sums to $0.02; none is profitable after gas (mean $0.013 per arbitrage).

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
| Total gross profit, single-tick upper bound (USD) | $0.02 |
| Total net profit, net-positive episodes only (USD) | $0.00 |
| Peak trade stays before the next tickSpacing boundary (single-tick math provably exact; crossing a boundary does not mean L changes) | 22.2% of episodes |

## Per pair

| Pair (fee tiers) | Threshold (bps) | Episodes | Net-positive | Max gap (bps) | Median duration (blocks) | Gross upper bound (USD) | Mean gas (USD) |
|---|---|---|---|---|---|---|---|
| 100-500 | 6 | 9 | 0 | 10.0 | 1 | $0.02 | $0.0134 |

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

- Gross profit is a SINGLE-TICK UPPER BOUND: constant product on virtual reserves (L, sqrtP) of the current tick, optimal size in closed form, perfect execution at the end-of-block state, no competition.
- In-range liquidity L is forward-filled from Swap events; Mint/Burn between swaps are not replayed.
- Gap threshold is the sum of the two fees (spec); the exact break-even 1/((1-fa)(1-fb))-1 is slightly higher and is what the profit formula uses, so a few gross episodes can have ~0 profit.
- States are end-of-block; intra-block gaps that were opened and closed within one block are invisible.
- Pairs are evaluated independently; simultaneous episodes on pairs sharing a pool compete for the same liquidity, so summed profits overstate what one arbitrageur could capture.
- Gas: one representative estimate per pair (see gas_assumption), priced at each episode's peak-block base fee.
- Profits in USDC with 1 USDC = 1 USD.

## Reproducibility

- Git commit `5a6404cc8d7f237bc3386132f480349d29c0ae7e`; RPC provider `arb-mainnet.g.alchemy.com`; registry verified at block 520884405.
- Full config in `config.json`; episodes in `episodes.parquet`.
