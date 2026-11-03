# Learning Log

For each component: what it does, why this implementation, assumptions,
what can go wrong, how to make it production-grade.

## Data Collectors

### Swap log collector (`data/collectors/swaps.py`)

**What it does.** Fetches every Uniswap v3 `Swap` event for the registry's pools over the
last N blocks (default 20,000, `configs/collector.yaml`). It decodes the amounts, sqrtPriceX96,
liquidity, tick, block number/timestamp, tx hash and log index, and caches them as Parquet in
`data/cache/swaps/<pool>/<from>_<to>.parquet`.

**Why this implementation.**
- *File names are the cache index.* Each file covers an inclusive block range, including
  ranges with zero swaps. Before fetching, the collector computes which ranges are missing
  per pool and fetches only those. No separate manifest can drift out of sync with the files.
  Writes go through a temp file plus `os.replace`, so a crash never leaves a partial file
  that looks complete.
- *One request serves all pools.* `eth_getLogs` takes a list of addresses. `plan_fetches`
  splits the window at every pool's gap boundaries, so each chunk asks only for the pools
  missing it.
- *Chunk size comes from the provider's real limit.* Alchemy's Free tier rejects
  `eth_getLogs` ranges over 10 blocks (measured 2026-11-03), so 20,000 blocks means
  2,000 requests (about 7 minutes at about 5 req/s). If a provider still rejects a range,
  the chunk is halved recursively.
- *Retries.* Exponential backoff on HTTP 429/5xx, timeouts and "rate limit" JSON-RPC
  errors. The first full run needed 883 retries for 2,000 chunks, all of which succeeded.
- *Big integers are stored as strings.* int256 amounts and the uint160 sqrtPriceX96 do not
  fit in 64-bit Parquet types. Storing them as floats would silently lose precision.
- *Timestamps come from the log.* The `blockTimestamp` field avoids one block request per
  block.

**Assumptions.**
- The window ends at the **finalized** block by default, not `latest`, so the cache never
  holds reorgable data. On Arbitrum, finalized lags latest by about 4,000 blocks (about
  16 min). "The last 20,000 blocks" therefore means the last 20,000 *finalized* blocks.
- The provider returns `blockTimestamp` in logs. Alchemy does, but it is a recent addition
  to the JSON-RPC spec. The decoder raises an error if it is missing rather than guessing.
- Detecting "range too large" errors relies on matching phrases in the error message
  (heuristic), because providers word it differently.

**What can go wrong.**
- On a provider with a cap on log *count* rather than block range, a busy range is split
  repeatedly. That works but is slow.
- With `head: latest`, a reorg after caching would leave stale rows. The collector only
  refuses logs flagged `removed`. It does not re-check cached blocks.
- If a pool is added to the registry later, only that pool's gaps are fetched, which is
  correct. But a pool whose address changes would leave orphaned cache directories.
- Many small requests use a lot of the free tier's compute units, and a long backfill can
  exhaust the monthly quota.

**Production-grade.** Use a paid or self-hosted Nitro node (wide ranges and higher
throughput), or a log indexer (e.g. a subgraph or HyperSync) for history. Persist a
`(block_number, block_hash)` table to detect reorgs. Add structured logging and metrics
(requests, retries, latency). Compact small files periodically. Test the error-path
heuristics against each provider's real messages.

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

### `data/pricing/v3_price.py`

**What it does.** Converts a pool's `sqrtPriceX96` into a human price (USDC per WETH),
taking token order and decimals from the registry.

**The conversion.**
1. `sqrtPriceX96` is √P in Q64.96 fixed point. Dividing by 2⁹⁶ gives √P, and squaring gives
   `P_raw = sqrtPriceX96² / 2¹⁹²`, the price of one smallest unit of **token0** in smallest
   units of **token1**.
2. To get whole tokens, multiply by `10^(decimals0 − decimals1)`.
3. On Arbitrum, token0 = WETH (address 0x82aF… < 0xaf88…), 18 decimals, and token1 = USDC,
   6 decimals. So `USDC per WETH = sqrtPriceX96² / 2¹⁹² × 10¹²`. If WETH were token1, the
   result would be inverted. `quote_per_base` decides this from the registry's token order,
   not from an assumption.
4. Worked example (the hand-checked test): `sqrtPriceX96 = 2⁸²`, so √P = 2⁻¹⁴, P_raw = 2⁻²⁸,
   and the price is 10¹² / 2²⁸ = **3725.2902984619140625** USDC/WETH exactly.
   Real example from the cache: `3781691934382028564530332` in the 0.05% pool gives about
   **2278.31** USDC/WETH.

**Why this implementation.** Python `Fraction` keeps every step exact. `sqrtPriceX96²` is
about 2³²⁰, far beyond float64's 53-bit mantissa, so the float form `(s/2**96)**2*1e12` is
only an approximation. The float column for analytics is computed exactly first and rounded
once.

**Assumptions.** The pool is a Uniswap v3-style pool whose `slot0`/`Swap` sqrtPriceX96 is
token1/token0 in raw units. This is a *mid* (spot) price: no fee, no price impact.

**What can go wrong.** Mixing up token order or decimals gives a price that is off by 10¹²
or inverted. Using the Swap's `sqrtPriceX96` as the "trade price" is wrong, because it is
the price *after* the swap. The trade's average price is `−amount1/amount0`, adjusted for
decimals and fee.

**Verification.** `tests/test_v3_price_onchain.py` reads `pool.slot0()` at the block of each
pool's latest cached swap. It asserts that `sqrtPriceX96` and the tick are *exactly* equal,
not merely equal within rounding. This works because in v3 only `swap()` moves the price,
so `slot0` at the end of block N equals the last Swap in block N.

**Production-grade.** Use vectorised exact math (e.g. integer arithmetic in Rust or numpy
object arrays) for millions of rows. Add an `effective_price` from swap amounts. Add
tick ↔ price helpers with tests against Uniswap's TickMath.

## Replay Engine

### `research/replay/` (`replay.py`, `gas.py`, `run.py`)

**What it does.** Replays the cached Swap logs block by block. It reconstructs each pool's
end-of-block state, measures the price gap for every pool pair, and flags blocks where the
gap exceeds the sum of the two fees. It sizes the optimal two-pool arbitrage under a
single-tick model and subtracts gas. It then groups consecutive flagged blocks into
episodes and saves metrics, episodes, config and charts to `research/results/<run_id>/`.

**Why this implementation.**
- *Segments instead of 20,000 rows.* State only changes in blocks with a swap. The window is
  split into about 670 constant-state segments, each weighted by the number of blocks it
  spans. This is exact and fast.
- *Seeded state.* A pool that does not swap early in the window gets its state from `slot0()`
  and `liquidity()` at `from_block - 1`, cached as Parquet. Forward-filling from nothing
  would leave it undefined.
- *Closed-form sizing.* Two chained constant-product swaps give `out(d) = a·d/(b + c·d)`,
  so the optimum is `d* = (√(ab) − b)/c` and the profit is `(√a − √b)²/c`. A test checks
  this against brute-force simulation.
- *Gas from NodeInterface.* `gasEstimateComponents` simulates a real SwapRouter02 round trip
  (WETH → USDC in one pool → WETH in the other), using a state-overridden sender balance.
  The router address is verified on-chain first. Replaying an observed arbitrage bot's
  transaction reverted, as such state-dependent transactions do, so a router call was the
  feasible representative transaction. The estimates are 251k–291k gas, L1 component 0,
  which is about $0.013 at the base fee of 0.02 gwei.
- *Censoring is explicit.* Episodes touching the window edges are flagged. Durations exclude
  episodes still open at the end.

**Assumptions.** End-of-block states (no intra-block view). Liquidity L is the value from
the pool's last Swap. Each pair is evaluated independently. 1 USDC = 1 USD. One gas probe
per pair, priced at each episode's peak-block base fee.

**What can go wrong / limitations.**
- **"Upper bound" is optimistic, not strict.** It assumes perfect execution at the end-of-block
  state with no competitor. But the single-tick model can *under*state profit if the trade
  crosses into a tick with more liquidity. It overstates profit if it crosses into less.
  `within_tick_range` only says whether the trade could reach the next tick that *might*
  be initialized. For the largest episode, an on-chain check of the 0.01% pool's ticks
  showed L unchanged for 8 ticks, so that episode's estimate is exact on that side.
- Mint/Burn between swaps change L without a Swap event, so the replayed L can be stale.
- Gaps opened and closed within one block are invisible. On Arbitrum's FCFS sequencer this
  is probably where most arbitrage happens, so the episode count is a lower bound on
  *opportunities* and says nothing about how fast bots react.
- Summing across pairs double-counts shared liquidity.
- SwapRouter02 gas probably overstates a direct-to-pool arbitrage contract.

**Production-grade.** Simulate multiple ticks with `tickBitmap` and `ticks()`, or call
QuoterV2 at the block, to get exact sizing. Replay Mint/Burn so L is right. Measure gas
from a Foundry-deployed arbitrage contract on a fork. Use per-transaction (intra-block)
ordering from receipts. Model competition and latency.

## Analytics

### `research/analytics/charts.py` and the metrics in `replay.headline_metrics`

**What it does.** Computes the headline metrics (N, H, S, G, M, P, duration quantiles, gross
vs net) and draws three PNG charts: per-block gap distribution per pair with the fee
threshold, episode duration histogram, and pool prices around the largest episode.

**Why.** The metrics come straight from `episodes.parquet`. The summary's headline sentence
is a template filled in from those numbers, so nothing in it is hand-written. Each pool has
a fixed colour across charts, and colours come from a colour-blind-validated palette.

**Assumptions.** Gap histograms weight each segment by the blocks it spans. Duration
quantiles use closed episodes only, with the nearest-rank method.

**What can go wrong.** With few episodes (9 in the first run), medians and p90 are noisy.
Quote them with the count. Arbitrum timestamps have 1-second resolution, so durations
under a second show as 0 s.

**Production-grade.** Run over many windows and report confidence intervals. Add an
interactive dashboard. Store metrics in a table to compare across runs.

## Contracts
