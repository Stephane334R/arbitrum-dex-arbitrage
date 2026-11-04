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

### Tick snapshot collector (`data/collectors/ticks.py`)

**What it does.** For one pool at one block, it reads `slot0`, `liquidity`, `fee`,
`tickSpacing`, and every `tickBitmap` word in a window around the current tick. It also reads
`ticks(t)` (liquidityGross, liquidityNet) for each initialized tick in that window. It returns a
`PoolSnapshot` for the swap port and caches the reads under
`data/cache/ticks/<pool>/<block>/` (`state.parquet` plus one `word_<pos>.parquet` per bitmap word).

**Why this implementation.**
- *The pool's own getters, not TickLens.* `tickBitmap(int16)` and `ticks(int24)` are public on
  every v3 pool, so there is no extra helper address to verify. The raw bitmap words are also
  needed anyway: the swap loop stops at word boundaries (see the swap port below).
- *One file per word is the cache index.* Widening the window (e.g. from ±2 to ±4 words)
  fetches only the new words. Files are written atomically (temp file + `os.replace`). A zero-row
  file means "this word was read and is empty", so it is not fetched again.
- *Checks on every fetch.* Each set bit must have `ticks().initialized == True` and
  `liquidityGross > 0`. On load, each tick must sit on a multiple of tickSpacing and belong to
  the word file it is in.

**Assumptions.** The window has a fixed size in words. One word covers 256 × tickSpacing ticks:
about ±2.6% of price for the 0.01% pool (spacing 1), and about ×4.6 for the 0.30% pool
(spacing 60). The state read is the state *after* the block. Reads use the pinned block number,
so they are consistent with each other.

**What can go wrong.**
- A thin pool runs past the window. The swap port then raises `TickDataOutOfRange`; the parity
  run widens the window automatically, up to a cap. At block 520884405, 10 WETH into the 0.01%
  pool crossed 221 initialized ticks, and 300 WETH ran past ±16 words.
- Dense words mean many calls: one `ticks()` call per initialized tick, at about 5 req/s on
  Alchemy's free tier.
- A block number that is later reorged would leave stale cache entries. Pinning a finalized
  block avoids this.

**Production-grade.** Batch the reads (Multicall3, or a verified TickLens) into one call per
word. Build the tick map once from Mint/Burn logs and update it incrementally instead of
re-reading storage at every block. Store the block hash next to the block number to detect
reorgs.

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
tick ↔ price helpers (done: `get_sqrt_ratio_at_tick` / `get_tick_at_sqrt_ratio` in `v3_swap.py`).

### `data/pricing/v3_swap.py`: exact port of the Uniswap v3 swap

**What it does.** Simulates `UniswapV3Pool.swap` on a `PoolSnapshot`, with the same integer
results as the deployed contract: amount in, amount out, fee, final sqrtPriceX96 and tick, final
liquidity, the initialized ticks crossed, and the number of loop steps. It also reproduces
QuoterV2's `initializedTicksCrossed` counter.

**The math, function by function.** Notation: price P = token1/token0 in raw units, √P is stored
as `sqrtPriceX96 = √P · 2⁹⁶`, L is the in-range liquidity.
- *FullMath.mulDiv / mulDivRoundingUp*: `floor(a·b/d)` / `ceil(a·b/d)` with a 512-bit
  intermediate, reverting if the result exceeds uint256. In Python, big ints make the
  intermediate free; only the overflow checks have to be added.
- *UnsafeMath.divRoundingUp*: `ceil(x/y)`; division by zero returns 0, as in the EVM.
- *LiquidityMath.addDelta*: `L + liquidityNet` with the pool's underflow ('LS') and overflow
  ('LA') checks.
- *TickMath.getSqrtRatioAtTick*: `√(1.0001^tick) · 2⁹⁶`. It is built from the binary digits of
  |tick|: one precomputed Q128.128 factor per bit, multiplied in and shifted. Positive ticks are
  inverted. The final conversion to Q64.96 rounds **up**, so tick → price → tick round-trips.
- *TickMath.getTickAtSqrtRatio*: the largest tick whose price is ≤ the given price. It takes
  log₂ from the most significant bit plus 14 fractional bits (square, check ≥ 2, shift), converts
  that to base √1.0001, and then picks between two candidate ticks with one exact
  getSqrtRatioAtTick.
- *SqrtPriceMath.getAmount0Delta*: token0 between two prices, `L · (√b − √a) / (√a · √b)`. It
  divides in **two** rounded stages (first by √b, then by √a); one exact division can differ by
  1 wei.
- *SqrtPriceMath.getAmount1Delta*: token1 between two prices, `L · (√b − √a) / 2⁹⁶`.
- *getNextSqrtPriceFromAmount0RoundingUp*: the price after adding or removing token0,
  `L·√P / (L ± x·√P)`, rounded up. If `x·√P` overflows 256 bits, Solidity switches to
  `L / (L/√P + x)`, which rounds differently. The port detects the overflow exactly as Solidity
  does (unchecked multiply, then `product / x == √P`).
- *getNextSqrtPriceFromAmount1RoundingDown*: the price after adding or removing token1,
  `√P ± x/L`, rounded down.
- *getNextSqrtPriceFromInput / FromOutput*: choose between the two above by direction. Rounding
  always favours the pool: an input never moves the price past what it pays for, and an output
  always moves it at least as far as needed.
- *SwapMath.computeSwapStep*: one step inside one liquidity range. Exact input: take the fee
  first (`amountRemaining · (1e6 − fee) / 1e6`), check whether that reaches the target price,
  and otherwise compute the next price from the input. If the target was not reached, the whole
  remainder counts as input + fee, so any rounding dust goes to LPs. Otherwise the fee is
  `ceil(amountIn · fee / (1e6 − fee))`.
- *TickBitmap.nextInitializedTickWithinOneWord*: the next initialized tick, but searched only
  inside the current 256-bit word. If the word has none, the loop still takes a step to the word
  boundary. Each extra step adds rounding, so an exact port needs the real word layout, not just a
  sorted list of ticks. In the parity run, 3000 WETH into the 0.30% pool crossed 211 ticks in 221
  steps: 9 stopped at an empty word boundary, and the last one ended inside a range.
- *swap loop*: repeat {find next tick (clamped to MIN/MAX_TICK), cap at the price limit,
  computeSwapStep, update remaining/calculated; if a tick boundary was reached, cross it and apply
  ±liquidityNet, then set tick = tickNext (− 1 when moving down); else recompute the tick from
  the price}.
- *PoolTicksCounter (QuoterV2)*: counts set bitmap bits between tickBefore and tickAfter and
  removes an endpoint that was not actually crossed. It uses truncating division, unlike
  TickBitmap. It is ported only to compare with the quoter; `SwapResult.ticks_crossed` is the
  real list.

**Why this implementation.** It is a line-by-line port of the canonical sources (v3-core
libraries + `UniswapV3Pool.swap`, v3-periphery `PoolTicksCounter`), not a re-derivation. The
details that change results by 1 wei were kept: two-stage rounding, the overflow-branch formula,
truncating signed division, word-boundary steps. Every `require` raises `SolidityRevert`.
Missing tick data raises `TickDataOutOfRange`, so a result is exact or an error, never an
estimate.

**Verification.**
- Offline (`tests/test_v3_swap_math.py`): TickMath against 90-digit Decimal values (error ≤ 1 +
  5·10⁻²⁰ relative), tick ↔ price round trips, FullMath against `Fraction`, amount deltas
  between exact floor and ceil, the overflow fallback branch, step invariants, the bitmap walk
  against brute force, and single-range swaps against closed-form formulas.
- On-chain (`research/parity/quoter_parity.py`, run `20261104T204637Z_3c7738a`): **30/30**
  QuoterV2 `quoteExactInputSingle` answers match exactly (amountOut, sqrtPriceX96After,
  initializedTicksCrossed) at block 520884405. That is 3 pools × 5 sizes × both directions,
  from 0.0001 WETH (no crossing) to 232 initialized ticks crossed. The QuoterV2 address was
  verified on-chain first (code, `factory()`, `WETH9()`). The vectors, with each pool's full
  tick snapshot, are in `tests/vectors/v3_swaps.json` and are replayed offline by
  `tests/test_v3_swap_parity.py`.

**Assumptions.** The pools run the canonical v3-core 1.0.0 bytecode, which is what the quoter
parity shows for these three pools. Fee-on-transfer and rebasing tokens are out of scope.
The exact-output path is ported and unit-tested but **not** parity-tested against the quoter.

**What can go wrong.**
- Forks of v3 (PancakeSwap v3, Sushi v3, Algebra) change fees, tick spacing or the step
  function. Re-run the parity before reusing the port on them.
- Running out of fetched words raises an error by design; widen the window.
- Python big-int math is slow (milliseconds per swap, more per crossing): fine for research, too
  slow for scanning many sizes per block.
- Parity at one block is strong evidence, not proof, for all states.

**Production-grade.** Re-run the parity at several blocks and on every pool before use. Fuzz
against the real contracts with Foundry using the JSON vectors. Port to Rust (e.g. `uniswap_v3_math`
or a hand port) for speed. Add the exact-output and multi-hop quoter checks.

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
- *Why the L1 component is 0 (measured, not assumed).* On Arbitrum, `gasEstimate` is the
  L2 execution gas plus an L1 data cost converted into L2 gas units. On 2026-11-03 that L1
  part was 0. The decoding was checked, not just trusted:
  - The selector `c94e6eeb` is `gasEstimateComponents(address,bool,bytes)`.
  - The call returns exactly 4 words in the documented order, `[259305, 0, 20156000, 0]`.
  - `gasEstimateL1Component` also returns 0, and setting a `gasPrice` changes nothing.

  The chain itself shows the L1 data price is zero:
  - `ArbGasInfo.getL1BaseFeeEstimate()` returns 0.
  - `getPricesInWei()` charges 0 per L1 calldata byte.
  - Real receipts in the latest block all have `gasUsedForL1 = 0`.

  The cause is Arbitrum's adaptive L1 pricing. `getL1PricingSurplus()` shows about
  0.408 ETH collected beyond what batch posting to Ethereum has cost. While there is a
  surplus, the L1 price is pushed down, here to zero. That is temporary: once the surplus
  is spent, the price rises again and transactions pay an L1 component. The replay calls
  NodeInterface again on every run, so it picks this up. But it probes **once per run**,
  at the window's last block. It would miss a change in L1 price *during* the window.
  `config.json` records `gas_units_l1` and `l1_base_fee_estimate_wei` so the zero is
  visible in each result.
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

**Production-grade.** Exact multi-tick sizing is now done at the peak block (see the next
section). Replay Mint/Burn so L is right. Measure gas
from a Foundry-deployed arbitrage contract on a fork. Use per-transaction (intra-block)
ordering from receipts. Model competition and latency.

### Exact multi-tick sizing (`research/replay/multitick.py`)

**What it does.** For each episode, it reads tick snapshots of the cheap and expensive pools at
the episode's peak block (`data/collectors/ticks.py`). It then finds the USDC input that
maximises `USDC out − USDC in` for the round trip USDC → WETH (cheap pool) → USDC (expensive
pool), running both legs through the exact swap port. Its size and gross profit replace the
single-tick ones in `episodes.parquet` and `summary.md`; the single-tick values stay as
`*_single_tick` columns. It also re-runs the single-tick formula with the *on-chain* liquidity
(`st_onchain_l_gross_usdc`), so the single-tick error splits into three causes: stale
liquidity, ticks crossed, and integer rounding.

**Why this implementation.**
- *Search, not a formula.* Across initialized ticks, profit has no closed form. Exact profit is
  concave in size, because each leg's marginal price only gets worse, so a doubling search
  followed by a ternary search finds the optimum in about 100 swap simulations per episode.
- *A noise floor.* Below about 1 USDC, wei rounding can make profit dip and recover, so the
  doubling search does not stop on a decrease before 2²⁰ raw units.
- *The on-chain state, checked against the replay.* The run fails if slot0's sqrtPriceX96 at the
  peak block differs from the replay's state, which it must not, because only swaps move the
  price. It records whether liquidity differs, which Mint/Burn can cause.
- *A same-window comparison.* The swap collector now accepts a fixed finalized `head` block, and
  `configs/replay.yaml` pins the first run's window. So the new run
  (`20261104T211446Z_c57f8b8`) re-finds the same 9 episodes, and the two numbers differ only
  in the sizing.

**Result.** The single-tick approximation gave $0.023422 gross over 9 episodes; the exact value
is $0.023342. It **overstated gross profit by $0.000080 (0.34%)**:
- $0.000074 from the one episode whose optimal trade crosses an initialized tick (−1.0% for
  that episode);
- $0.000006 of integer rounding over the other 8;
- ≈ $0 from stale liquidity: one episode had a Mint/Burn since the last swap, with a
  negligible effect.

The approximation was good here because these arbitrages are tiny (about $1–$57 of input).
It will matter more for larger gaps, and in thin pools such as the 0.01% pool, where 10 WETH
crosses 221 ticks.

**Assumptions.** The peak block is still chosen with the single-tick gross, as before;
recomputing every above-threshold segment exactly would cost one tick snapshot per segment.
The optimal size is accurate to a few raw units (a few 10⁻⁶ USDC), because rounding breaks
exact concavity at that scale. It assumes perfect execution at the end-of-block state with no
competitor, as before.

**What can go wrong.**
- If the peak by exact profit is a different segment than the peak by single-tick profit, the
  episode's exact peak is understated.
- A large arbitrage in a thin pool needs a wide tick window. The window widens automatically up
  to `max_words_each_side`, then the run fails rather than guessing.
- Each new peak block costs one tick snapshot per pool: one `tickBitmap` call per word plus one
  `ticks()` call per initialized tick. Measured in this run's cache, the median is 12
  initialized ticks per word for the 0.01% pool and 256 (every tick) for the 0.05% pool. The
  results are cached afterwards.

**Production-grade.** Size every above-threshold segment exactly and choose the peak by exact
profit. Update tick maps incrementally from Mint/Burn logs instead of re-reading storage per
block. Optimise the full route (both directions, all pairs together, shared-pool contention).
Check the chosen sizes against a fork simulation of the real arbitrage contract.

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
