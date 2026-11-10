# Replay 20261109T192320Z_a42a515

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
- Block-level states are end-of-block; gaps opened and closed within one block are invisible to the block-level replay (the transaction-level section, when enabled, measures them).
- Pairs are evaluated independently; simultaneous episodes on pairs sharing a pool compete for the same liquidity, so summed profits overstate what one arbitrageur could capture.
- Gas: one representative estimate per pair (see gas_assumption), priced at each episode's peak-block base fee.
- Profits in USDC with 1 USDC = 1 USD.

## Reproducibility

- Git commit `a42a515e66bae17b2598568b05c88253abf45a5c` (with uncommitted changes); RPC provider `arb-mainnet.g.alchemy.com`; registry verified at block 520884405.
- Full config in `config.json`; episodes in `episodes.parquet`.

## Transaction-level replay

State rebuilt after each of 1,169 transactions touching the pools (1,140 Swap, 20 Mint, 69 Burn events), ordered by (block, transaction index, log index).

### Data quality

Replayed end-of-block state vs archive `slot0()`/`liquidity()` at 120 (block, pool) checkpoints: every 500 blocks plus the window's last block, 40 blocks (listed in config.json), checked for every pool.

| Check | Mismatches |
|---|---|
| Any field (sqrtPriceX96, tick or liquidity): **mismatch rate** | 0 / 120 (0.00%) |
| sqrtPriceX96 | 0 |
| tick | 0 |
| liquidity, transaction-level (Swap + Mint/Burn) | 0 |
| liquidity, swap-only (what the block-level replay uses) | 19 |

### Block-level vs transaction-level

| Metric | Block-level | Transaction-level |
|---|---|---|
| Episodes | 9 | 17 |
| Net-positive episodes | 0 | 2 |
| Gross profit, exact multi-tick, one capture per episode (USD) | $0.0233 | $0.1030 |
| Opened and closed inside one block | (invisible) | 8 (47.1% of closed) |
| Median / p90 duration, transactions on the pair's pools | n/a | 1 / 5 |
| Median / p90 duration, blocks (transaction level: close - start block, 0 = same block) | 1 / 10 | 1 / 5 |
| Closing transaction trades both pools of the pair | n/a | 5.9% of closed |

**Missed by the block-level replay: 8 of 17 transaction-level episodes (47.1%)**, with exact gross $0.0780 (75.8% of the transaction-level total) and 2 net-positive.
Their peak gaps: median 8.88 bps, max 13.18 bps; median gross $0.005941, max $0.036979. Median duration 1 transaction(s) on the pair's pools, median 7 transaction indices between opener and closer.

The 9 visible transaction-level episodes map onto 9 block-level episodes; 0 block-level episode(s) contain more than one transaction-level episode (the gap closed and reopened inside a block). Consistency check: the blocks at whose end a transaction-level episode is open equal the block-level episode blocks (**passed**).
Exact multi-tick tick table exact for 17 of 17 episodes (see simplifications).

| Pair | Start (block / tx idx) | Close (block / tx idx) | Txs | Gap (bps) | Gross, exact (USD) | Net (USD) | Block-level | Closer trades both pools |
|---|---|---|---|---|---|---|---|---|
| 100-500 | 520864964 / 2 | 520864965 / 1 | 1 | 6.07 | 0.000002 | -0.013246 | episode #0 | no |
| 100-500 | 520865505 / 5 | 520865505 / 8 | 1 | 9.12 | 0.007050 | -0.006590 | missed | no |
| 100-500 | 520871267 / 1 | 520871267 / 2 | 1 | 7.43 | 0.001450 | -0.011753 | missed | no |
| 100-500 | 520871434 / 76 | 520871435 / 27 | 5 | 7.39 | 0.001376 | -0.011983 | episode #1 | no |
| 100-500 | 520871441 / 18 | 520871442 / 9 | 3 | 7.28 | 0.001161 | -0.012056 | episode #2 | no |
| 100-500 | 520871442 / 65 | 520871442 / 72 | 2 | 6.55 | 0.000213 | -0.013298 | missed | no |
| 100-500 | 520871443 / 19 | 520871446 / 2 | 15 | 9.52 | 0.008905 | -0.004416 | episode #3 | no |
| 100-500 | 520871448 / 1 | 520871448 / 14 | 1 | 6.02 | 0.000000 | -0.014687 | missed | no |
| 100-500 | 520871516 / 1 | 520871516 / 35 | 4 | 11.87 | 0.024677 | 0.011090 | missed | no |
| 100-500 | 520871517 / 41 | 520871522 / 19 | 7 | 6.71 | 0.000356 | -0.013891 | episode #4 | no |
| 100-500 | 520871527 / 25 | 520871528 / 23 | 3 | 9.97 | 0.011279 | -0.002023 | episode #5 | no |
| 100-500 | 520871872 / 3 | 520871872 / 6 | 1 | 8.88 | 0.005941 | -0.007348 | missed | no |
| 100-500 | 520872967 / 1 | 520872967 / 19 | 1 | 7.54 | 0.001687 | -0.011725 | missed | no |
| 100-500 | 520872986 / 2 | 520873027 / 2 | 3 | 7.17 | 0.000979 | -0.012294 | episode #6 | no |
| 100-500 | 520873453 / 1 | 520873463 / 1 | 1 | 6.79 | 0.000442 | -0.012832 | episode #7 | no |
| 100-500 | 520873629 / 1 | 520873629 / 3 | 1 | 13.18 | 0.036979 | 0.023730 | missed | yes |
| 100-500 | 520874791 / 2 | 520874792 / 3 | 1 | 6.80 | 0.000459 | -0.012859 | episode #8 | no |

### Transaction-level simplifications

- Transaction-level state is rebuilt from Swap (exact post-swap sqrtPriceX96, tick, liquidity) and Mint/Burn (liquidity +/- amount when tickLower <= tick < tickUpper). Checked against archive slot0()/liquidity() at the checkpoint blocks listed in config.json, not at every transaction.
- Swap transaction indices were looked up by tx hash (eth_getTransactionByHash) because the swap cache has no index column; Mint/Burn indices come from their logs.
- Exact multi-tick gross at the peak transaction uses the tick table at the end of the previous block with the replayed intra-block price/tick/liquidity; exact unless a Mint/Burn of that pool earlier in the same block changed an initialized tick (mt_tick_table_exact).
- An intra-block opportunity can only be taken by a transaction ordered right after the opener in the same block. The gross figures assume that slot was available to us: they measure what existed, not what was capturable.
- duration_txs counts transactions touching the pair's pools only; tx_index_distance (same-block episodes) counts every transaction in the block between opener and closer.
