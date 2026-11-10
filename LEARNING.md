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
- *Gas from NodeInterface (superseded on 2026-11-09 by the executor's forge gas plus per-block
  NodeInterface intrinsic/L1 gas; see "Gas from the executor" below).* `gasEstimateComponents` simulates a real SwapRouter02 round trip
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

### Transaction-level replay (`research/replay/txlevel.py`, `data/collectors/liquidity_events.py`, `data/collectors/tx_index.py`)

**What it does.** It runs next to the block-level replay. It rebuilds each pool's state after
*every transaction*, not once per block, and then finds gaps and episodes with the same rules.
Steps:
1. Collect Mint and Burn logs for the same pools and window, cached like the Swaps.
2. Merge them with the Swaps and sort by (block, transaction index, log index).
3. Replay the state. A Swap sets sqrtPriceX96, tick and liquidity to the values in its event. A
   Mint or Burn adds or removes `amount` from liquidity when `tickLower <= tick < tickUpper`.
4. Every `snapshot_every_blocks` blocks, compare the replayed state with archive
   `slot0()`/`liquidity()` and report the mismatch rate.
5. Find episodes. One can now open and close inside a single block. Durations are measured in
   transactions, in blocks and in seconds.
6. Size each episode exactly at its peak transaction, then compare with the block-level
   episodes: which ones the block level saw, which it missed, and how big the missed ones were.

**Why this implementation.**
- *Why it matters for competition.* On Arbitrum the sequencer orders transactions by arrival
  (first come, first served, plus the Timeboost express lane). A searcher does not wait for the
  end of a block. They send a backrun as soon as they see the transaction that opened the gap.
  If the backrun lands right after it in the same block, the gap never exists at a block
  boundary. So the block-level replay mostly sees gaps that *nobody* closed fast. Those are the
  leftovers, not the competition. The transaction-level replay sees the gaps that were closed:
  how many there were, how big they were, how many transactions they survived, and whether the
  closing transaction traded both pools (`closer_touches_both_pools`, the mark of an arbitrage).
  This turns "how often are pools out of line at block ends?" into "how often does an
  opportunity appear, and how fast does someone take it?".
- *Exact state, checked against the chain.* A Swap event carries the pool's post-swap slot0
  price, tick and in-range liquidity. Mint and Burn follow the pool's own `_modifyPosition`
  rule. No other function moves those three values. So with complete logs the replay is exact,
  and the archive checkpoints test that claim instead of assuming it. The same checkpoints
  also count how often the block level's swap-only liquidity was stale.
- *Transactions, not events.* State is recorded after each transaction. A state in the middle
  of a transaction (between the two legs of someone else's arbitrage) cannot be traded against.
- *Per-pair rows.* Only transactions that touch one of a pair's two pools can change its gap.
  So `duration_txs` counts those transactions, and `tx_index_distance` (same-block episodes
  only) counts every transaction in between.
- *Transaction index without re-fetching swaps.* The swap cache stored `logIndex` but not
  `transactionIndex`. Re-fetching 20,000 blocks of Swaps would break the cache rule, so each swap
  tx hash is looked up once (`eth_getTransactionByHash`) and cached. `check_order` then checks
  that the order by (tx index, log index) matches the block-wide logIndex, and that each tx hash
  has a single index.
- *Built-in cross-check.* Both replays read the same exact prices, so the blocks at whose end a
  transaction-level episode is open must equal the block-level episode blocks. The run fails
  otherwise.
- *Shared collector.* `swaps.py` now takes a `LogSpec` (event topics, decoder, schema, cache
  root), so Mint/Burn reuse the same range bookkeeping, chunking, retry and atomic writes.

**Assumptions.**
- The logs are complete: every Swap/Mint/Burn of the pools is in the cache.
- Logs from finalized blocks only (as for the swaps).
- Exact multi-tick sizing at the peak transaction reads the tick table at the end of the
  previous block and replaces price, tick and liquidity with the replayed values. That is exact
  unless a Mint/Burn of that pool earlier in the same block changed an initialized tick, which
  `mt_tick_table_exact` flags.
- Gross profit at an intra-block state assumes we could have placed our transaction right
  after the opener. It measures what existed, not what was capturable.

**What can go wrong.**
- A missing log (provider gap, a pool event signature not collected) silently corrupts the state
  until the next Swap. Liquidity is the field at risk, because Swaps reset price and tick. The
  checkpoints catch it, but only at checkpoint blocks. A Burn that drives liquidity negative
  raises immediately.
- Tick-boundary edge cases: a Mint whose `tickUpper` equals the current tick is out of range.
  The pool's rule is lower-inclusive, upper-exclusive, and this is tested.
- An event emitted by another contract with the same signature (e.g. a v3 fork) is filtered
  out by the `address` filter, but a registry mistake would let it in.
- Same-second timestamps: most intra-block durations are 0 s, so the transaction-based
  durations are the useful ones.

**Production-grade.**
- Stream logs live (websocket or the sequencer feed) and update the state incrementally.
  The replay is already an event-driven state machine, so the same `apply_event` can run on
  live data.
- Persist the tick table and update it from Mint/Burn too, so intra-block multi-tick sizing is
  exact without archive reads.
- Measure latency directly: the sequencer feed gives arrival order. Pending-transaction
  timestamps would show how many milliseconds a backrun had.
- Classify closers (known bot contracts, router vs direct pool calls) to see who wins and at
  what gas price, and model a realistic capture rate instead of assuming every gap is ours.

**First result (20,000-block window, run `20261109T192320Z_a42a515`, uncommitted tree, old
SwapRouter gas).**
- 0 of 120 archive checkpoints mismatched. The swap-only liquidity the block level uses was
  stale at 19 of 120.
- 17 transaction-level episodes vs 9 block-level. The 8 missed ones (all intra-block, median 1
  transaction on the pair's pools) hold 76% of the gross.
- The consistency check passed.

**24-hour result (run `20261109T213505Z_a42a515`).**
- 0 of 492 (block, pool) checkpoints mismatched on any field. Swap-only liquidity was stale at
  37 of 492.
- 542 transaction-level episodes against 312 block-level. The block level missed 226 (41.7%),
  all intra-block. They hold $1.23 of the $2.15 transaction-level gross (57%) and 15 of the 23
  net-positive episodes. Median missed gap 7.3 bps, largest 37.2 bps.
- The consistency check passed. 4 block-level episodes contain more than one transaction-level
  episode, where the gap closed and reopened inside one block.

### Gas from the executor, L1 price per peak block, sensitivity (`research/replay/gas.py`)

**What it does.** Gas per episode is now
`execute() L2 gas (forge) + intrinsic L2 gas + L1 data gas`, priced at the peak block's base
fee:
- The `execute()` figure is parsed from the saved fork-test run's `config.json`
  (`gas.synthetic_execute` = 340,692).
- Intrinsic and L1 gas come from an archive `NodeInterface.gasEstimateComponents` call at
  **each episode's peak block**. The call carries the episode's own `execute(...)` calldata and
  targets an address with no code, so only intrinsic and L1 gas are charged.
- At the same block it records `ArbGasInfo.getL1BaseFeeEstimate()` and `getPricesInWei()`.

A sensitivity table gives M (net-positive episodes) and total net at gas ×0.5, ×1 and ×2, for
both replay levels.

**Why this implementation.**
- *The contract we would actually use.* The SwapRouter02 probe measured someone else's code
  path: router overhead, path decoding, WETH wrapping. The executor's own gas is what matters.
- *Which forge number.* The gas report's `execute()` row aggregates 269 calls: reverts
  (min 22,839) and large fuzz sizes (median 704,361). None of them is a real trade. The
  fork-test runner already isolates the one successful SYNTHETIC `execute`, so that is the
  number used. The parser refuses a figure that aggregates more than one call.
- *Forge leaves out what the transaction adds.* Forge measures the call frame only. Intrinsic
  gas and calldata are added from NodeInterface, measured rather than computed by formula. It
  returned 22,543–22,579, while the geth rule (21,000 + 4/16 gas per calldata byte) gives
  22,244 for 164 bytes. Using the measured value avoids guessing Nitro's exact rule.
- *L1 price per block, not per run.* The old probe ran once, at the window's last block. Arbitrum's
  L1 price follows its batch-posting surplus and can change within a day. An archive call per
  peak block shows it directly, and the result is cached by (block, calldata).
- *Sensitivity instead of false precision.* The single largest uncertainty is the execution
  gas of a small trade. ×0.5 to ×2 brackets it, and the conclusion should be read off the table,
  not off ×1.

**Assumptions.**
- 340,692 gas is for one 50,000-USDC SYNTHETIC trade (0.30% → 0.05%) that crosses many
  initialized ticks. The replay's arbitrages are a few USDC and cross few or none, so this
  probably overstates their gas. ×0.5 is the plausible lower case.
- Same gas for every pair, including Uniswap–Sushi pairs, which the executor cannot trade as
  deployed (see the Sushi section).
- Base fee = the block header's `baseFeePerGas` (what the block charged). NodeInterface's own
  `baseFee` output is 0.0200 gwei at blocks whose header says up to 0.0215 gwei. Both are saved.
  The header value is used because it is the block's actual price.
- No priority fee. Arbitrum's sequencer ignores tips for ordering (FCFS); Timeboost express-lane
  bids are not modelled.

**What can go wrong.**
- If the executor changes, the gas must be re-measured: re-run `research.forktests.run` and point
  `gas.forktest_run` at the new run.
- `NodeInterface` is a virtual contract. A provider that does not serve archive `eth_call`s to it
  fails the run loudly; there is no silent fallback any more.
- Measured L1 gas was 0 at every peak block so far (the surplus regime). If that changes, gas
  rises for every episode, which is exactly what per-block recording is for.

**24-hour result.**
- 222 distinct peak blocks were probed. L1 data gas was 0 at every one, and so were
  ArbGasInfo's L1 base fee estimate and its price per calldata byte.
- Total gas was 363,235–363,271 units at 0.0200–0.0219 gwei, which is $0.0163–0.0181 per
  arbitrage.
- Sensitivity, M at ×0.5 / ×1 / ×2:
  - block level: 24 / 4 / 0;
  - transaction level: 62 / 23 / 8.
- Only 139 of the 312 block-level episodes are on Uniswap-only pairs; M at ×1 among them is 2.
- The conclusion does not hinge on the gas figure: even at ×0.5 the block-level net is $0.13
  per day.

**Production-grade.** Measure gas per trade size and per pair on a fork: a real `execute` at
each episode's peak block, with the episode's size. Add Timeboost bid costs. Model the
priority-fee or bid competition among searchers instead of base fee only.

### 24-hour window: one-pass log collection and batched tick reads

**What it does.**
- The replay window is now 24 h of blocks (520,575,466 – 520,881,828, 306,363 blocks), ending at
  the first run's last block, so the old window is its last part. The start block comes from a
  binary search over cached headers (`data/collectors/blocks.first_block_at_or_after`).
- `data/collectors/pool_logs.py` fetches Swap and Mint/Burn together: one `eth_getLogs` per
  chunk with an OR-list of topics.
- Swap rows now carry `transactionIndex` from the log.
- Tick snapshots read `ticks()` through Multicall3.

**Why this implementation.**
- *Requests are the bottleneck.* Alchemy Free caps `eth_getLogs` at 10 blocks, so 24 h is about
  30,600 requests at roughly 5 per second (about 1h45). Separate Swap and Mint/Burn passes would
  double that. The generalised `collect(specs=[...])` asks each chunk only for the (event, pool)
  pairs still missing, and drops logs for any (event, pool) range already cached, so the cache
  rule still holds.
- *No more per-hash lookups.* Old swap files keep using the cached tx-index lookup. New files
  have the index in the log, which saves about 1 request per swap transaction.
- *Multicall3 for ticks.* A 0.05%-pool tick snapshot has about 770 initialized ticks. One
  `ticks()` call each took minutes per episode; batched, it is 27 calls in about 5 s. The
  result was identical to the direct-call snapshot of the same block. Multicall3's address is
  canonical but taken from memory, so it is checked at use: code at the block,
  `getChainId() == 42161`, and every word's first tick is re-read directly and compared.

**Assumptions.** The provider accepts an OR-list of topic0 values (standard JSON-RPC). The
24 h window is defined by block timestamps: the first block whose timestamp is
≥ end − 86,399 s, so the window covers 86,400 one-second timestamp slots.

**What can go wrong.**
- Sharing the rate limit: anything else run during the fetch (tests, replays) gets HTTP 429.
  The integration tests' web3 fixture has no retry and errored during the fetch, then passed
  afterwards.
- A provider that returns logs for unrequested topics would previously have been decoded as
  the wrong event. Logs are now routed by topic0, and unknown topics raise.
- Transient provider errors on long runs:
  - The 24 h fetch stopped once on HTTP 429 after 8 retries. Progress was cached, and it
    resumed with `rpc_max_retries: 20`.
  - The replay then failed twice. Once on a 429 in the web3 provider, which had no retry at
    all. Once on Alchemy's JSON-RPC error `-32000 layer stale`.
  - Now both paths retry. The web3 session retries 429/5xx with backoff (POST included; the
    repo only sends read calls). `RpcClient` treats "layer stale" as transient. Other JSON-RPC
    errors still raise at once.

**Result.** 306,363 blocks hold 25,154 swaps and 957 Mint/Burn events over 4 pools: 0.01%
5,359 / 11, 0.05% 16,691 / 862, 0.30% 841 / 78, Sushi 0.05% 2,263 / 6. The resumed fetch made
23,588 requests (34,953 calls including retries) at 5.9 requests per second.

**Production-grade.** A paid plan or own node (wide `eth_getLogs` ranges), or an indexer. With
the collector this simple, a week of data is a config change; the limit is RPC budget.

### Who closed the gaps (`research/analytics/closers.py`, `data/collectors/tx_details.py`)

**What it does.** For every closed transaction-level episode, it takes the transaction that
ended it. That is the first transaction on the pair's pools after which the gap was back under
the threshold. For each one it fetches the sender, `to`, transaction index and gas used
(transaction + receipt, cached). It then ranks senders and `to` contracts by gaps closed,
reports the top-5 shares, and gives the median delay between opening and closing, in
transactions and in blocks. It also reads each closer's receipt and classifies it by the pools
it swapped in:
- *both pools of the pair*;
- *one of them plus other pools*: cross-venue arbitrage or a multi-hop route;
- *only one of them*: a directional trade or CEX-DEX arbitrage.

The tables and a bar chart of the top `to` contracts go into `summary.md`; every row is in
`closers.parquet`.

**Why this implementation.**
- *The replay already knows the closer.* `find_tx_episodes` records the closing transaction's
  hash, block and index. The new code only needs one transaction and one receipt per closer.
  It checks that the chain's block and index for that hash equal the replay's, which catches
  any ordering bug in the transaction-level replay.
- *Sender and contract both.* Searchers often rotate many EOAs behind one contract, so a
  sender ranking alone understates concentration. The `to` ranking (and the number of senders
  per `to`) shows it.
- *Arbitrage signature next to every number.* "Closed the gap" is a definition, not an intent.
  The share of closers that traded both pools of the pair in one transaction separates
  arbitrage from flow that happened to push the price back.
- *Why the receipt classification was added.* The first 24 h run showed 0.2% "traded both
  pools". On its own, that reads as "nobody arbitrages", which the closers' look contradicts:
  vanity contract addresses, non-ABI selectors such as `0x00000001`, and early positions in
  the block. The receipt shows the other pools the closer swapped in, so it can tell
  "arbitrage elsewhere" from "a single swap". Swap topics are computed from their signatures:
  Uniswap v2/v3/v4 shapes (the v3 one also covers Sushi and Algebra), PancakeSwap v3 and
  Balancer v2. The list is not exhaustive, so multi-pool is a lower bound.
- *Chart by contract.* Concentration is a contract-level fact (see the result), so the chart
  ranks `to` contracts. The sender table is still in the summary.

**Assumptions.** One closer per episode, the transaction that crossed back under the threshold.
A searcher whose trade only narrowed the gap, before someone else crossed it, gets no credit.
Gas used is the receipt's total; on Arbitrum that includes `gasUsedForL1`, which is stored
separately.

**What can go wrong.**
- Small samples: with tens of closed episodes, a top-5 share is noisy. Read it as indicative.
- Aggregators and routers as `to` (e.g. a public router) mix many users. A popular router at
  the top of the `to` ranking means retail flow, not one searcher.
- An arbitrage that routes through a pool outside the registry (e.g. another DEX) can close a
  gap while touching only one of the pair's pools. It is then not flagged as "traded both pools".

- The receipt classification cannot separate intents within a class. A multi-hop aggregator
  route looks like a cross-venue arbitrage, and a CEX-DEX arbitrage looks like a directional
  swap. Both readings are named in the summary.

**24-hour result (542 closed episodes, 432 distinct closing transactions).**

| Measure | Value |
|---|---|
| Closed by the very next transaction on the pair's pools | 76% |
| Closed inside the block they opened in | 42% |
| Median delay | 1 transaction, 1 block |
| Median closing index in the block | 3 |
| Distinct senders / distinct `to` contracts | 172 / 34 |
| Gaps closed by the top 5 senders | 25.6% |
| Gaps closed by the top 5 `to` contracts | 76.0% |
| Top sender `0x5556…4Fe2` | 16.4%, through contract `0xeF0D…4834` |
| Top contract `0xEa84…61A2` | 21.6%, 50 senders, always a single swap in one of our pools |

| Closer kind | Count | Share | Gross of those gaps |
|---|---|---|---|
| Traded both pools of the pair | 1 | 0.2% | $0.04 |
| One of our pools plus other pools (Uniswap v4, v2, PancakeSwap v3, …) | 194 | 35.8% | $0.72 |
| Only one of our pools | 347 | 64.0% | $1.39 |

No closer reverted.

*What it says about competition.*
- **Speed.** A gap is closed by the next transaction that touches the pool, usually early in a
  block. Taking one requires being ordered right behind the opener (the FCFS race or
  Timeboost), not reacting to end-of-block state.
- **Concentration sits behind contracts.** A sender ranking looks open: 172 senders, top 5 at
  26%. A handful of contracts, each fed by several EOAs, closes three quarters of the gaps.
- **These pairs are not where the arbitrage happens.** Two-pool arbitrage of exactly our pairs
  is almost absent. The gaps are closed by bots and flow that trade our pools against other
  venues, or against an off-chain price. The thin 0.01% pool (in most of the pairs) follows the
  market through those trades rather than through arbitrage against the 0.05% pool. For a
  two-pool v3 executor, this says the opportunity set is small and already served by faster,
  wider-venue searchers.

**Production-grade.**
- Label known contracts: routers, aggregators, known searcher contracts.
- Decode the closer's calldata and internal calls to measure the profit the closer actually
  made.
- Compare closer gas price and priority to non-closers. With Timeboost, also check
  express-lane rounds.

### SushiSwap v3 WETH/USDC (optional item)

**What it does.** Adds the SushiSwap v3 WETH/USDC 0.05% pool
(`0xf3Eb87C1F6020982173C908E7eB31aA66c1f0296`) to the registry next to the three Uniswap
pools. Its pairs (`500-sushi500`, etc.) go through the same replay.

**Verification (all passed).**
- *Factory address source.* docs.sushi.com/contracts/clamm renders its address table from a
  JS constant, not from the page, so the address was read from Sushi's official `sushi` npm
  package (v7.6.1), `evm/config/features/sushiswap-v3.ts`:
  `SUSHISWAP_V3_FACTORY_ADDRESS[ARBITRUM] = 0x1af415a1…231e`. Arbitrum is not in the
  package's deprecated list.
- *On-chain checks at block 520,881,828.* The factory has code. `getPool(WETH, USDC, fee)`
  returns a pool for 100, 500, 3000 and 10000. Each pool's `factory()`, `fee()`,
  `tickSpacing()` (= the factory's `feeAmountTickSpacing`) and tokens match.
- *Callback interface.* Each pool's bytecode contains `PUSH4 0xfa461e33`, the
  `uniswapV3SwapCallback(int256,int256,bytes)` selector the executor implements. That is a
  bytecode check, not a proof.
- The registry builder re-runs all of this, including the callback check, for every pool, and
  re-verified the set at block 522,767,219.

**Why only the 0.05% tier.** In-range liquidity at block 520,881,828 was 3.4e16 for 0.05%,
against 2.5e10 (0.01%), 1.0e12 (0.30%) and 2.0e10 (1%). The others are dust; including them
would add pairs whose "gaps" nobody can trade.

**What it does not do (labelled).**
- **V3ArbExecutor cannot trade Uniswap–Sushi pairs as deployed.** It checks both pools against
  one immutable factory (Uniswap), so a Sushi pool reverts with `PoolNotFromFactory`. Episodes
  on those pairs are real price gaps but carry `executable_by_executor = false`. Trading them
  needs a second trusted factory in the contract, plus fork tests.
- Exact multi-tick sizing uses the Uniswap v3 swap port on the Sushi pool. The pool is a fork
  with the same callback and getters, but QuoterV2-style parity against Sushi's own quoter has
  not been run. The tick-math test does cover all cached Sushi swap logs.

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

### `contracts/src/V3ArbExecutor.sol` + fork tests (`contracts/test/`)

**What it does.** `execute(poolA, poolB, zeroForOne, amountIn, minProfit)` runs a two-pool
round trip on a Uniswap v3 pair in one transaction. It sells exactly `amountIn` on poolA,
sells everything poolA returned on poolB in the opposite direction, repays both pools inside
their swap callbacks, and keeps the difference. The profit is in poolA's input token and goes
straight to the owner. It reverts if the profit is below `minProfit`.

Callback flow (nested flash swaps, so no capital is needed):

```
owner ─ execute()
         ├─ verify poolA, poolB via factory.getPool(token0, token1, fee); same pair; distinct
         ├─ balanceBefore = tokenIn.balanceOf(this)
         ├─ _expectedPool = poolA
         └─ poolA.swap(this, zeroForOne, +amountIn, no limit, data{stage A})
              ├─ poolA → this: tokenOut (received)
              └─ poolA → uniswapV3SwapCallback (stage A)
                   ├─ msg.sender == _expectedPool? then clear it (single use)
                   ├─ _expectedPool = poolB
                   ├─ poolB.swap(this, !zeroForOne, +received, no limit, data{stage B})
                   │    ├─ poolB → this: tokenIn
                   │    └─ poolB → uniswapV3SwapCallback (stage B)
                   │         ├─ msg.sender == _expectedPool? clear it
                   │         ├─ owed == received? (else PartialFill)
                   │         └─ pay poolB `owed` tokenOut
                   ├─ tokenIn balance >= owed to poolA? (else CannotRepay)
                   └─ pay poolA `owed` tokenIn
         ├─ profit = balanceAfter - balanceBefore; >= minProfit? (else InsufficientProfit)
         └─ transfer profit to owner; emit ArbExecuted
```

Each pool sends its output before calling the callback and checks its own balance after the
callback returns ('IIA' if underpaid). So poolB's output is already in hand when poolA must be
repaid. Each pool is locked during its own swap, but nesting different pools is allowed.

Revert paths, in the order they can trigger:

| Where | Revert | Cause |
|---|---|---|
| execute | `NotOwner()` | caller is not `owner` |
| execute | `Reentrancy()` | execute re-entered (transient `_locked`) |
| execute | `ZeroAmount()` | `amountIn == 0` |
| execute | `AmountTooLarge(amountIn)` | `amountIn > type(int256).max` (swap takes `int256`) |
| execute | `SamePool()` | `poolA == poolB` |
| execute | `PoolNotFromFactory(pool)` | no code at the address, or `factory.getPool(token0, token1, fee) != pool` |
| execute | raw revert from `pool.token0()` etc. | a contract with no such getters (e.g. a fallback that reverts) |
| execute | `PairMismatch(poolA, poolB)` | factory pools of different pairs |
| callback | `UnauthorizedCallback(caller)` | caller is not the one pool authorised right now (also when nothing is in flight) |
| callback | `InvalidDeltas(d0, d1)` | sign pattern does not match the swap direction |
| poolB | `'AS'` | poolA returned 0 output (dust `amountIn`), so poolB is asked to swap 0 |
| callback B | `PartialFill(owed, expected)` | poolB stopped early (price limit / out of liquidity); tokenOut would be stranded |
| callback A | `CannotRepay(balance, owed)` | poolB's output < what poolA is owed: the route loses money at this size |
| pool | `'IIA'` | a pool was underpaid (cannot happen once the checks above pass; it backs them up) |
| execute | `UnauthorizedCallback(pool)` | swap returned without calling back (not a genuine v3 pool) |
| execute | `Loss(before, after)` | tokenIn balance went down. Only reachable if pre-existing balance helped repay |
| execute | `InsufficientProfit(profit, minProfit)` | the gain is below the caller's threshold |
| any transfer | `TransferFailed(token, to, amount)` | token returned false, reverted, or has no code |
| rescue* | `NotOwner()` / `TransferFailed` | as above |

Any revert undoes the whole transaction, including both swaps. So the contract either
gains at least `minProfit` or nothing happens (apart from gas).

**Why this implementation.**
- *Nested flash swaps instead of pulling funds from the owner.* The pool sends its output
  before asking to be paid, so the arbitrage needs no capital and no approvals. The contract
  never holds a balance across transactions.
- *Two layers of callback authentication.* First, `execute` checks each pool with
  `factory.getPool` against the pool's own `token0/token1/fee`. Second, only that pool may
  call the callback, and only once (`_expectedPool` is cleared on entry). Even a genuine
  factory pool cannot call the callback unless it was authorised for this step. `data` is
  trusted only after the `msg.sender` check, because a genuine pool forwards it unchanged.
- *Transient storage (EIP-1153)* for `_expectedPool` and `_locked`. They reset at the end
  of every transaction, so no stale authorisation can survive a revert, and they cost no
  storage gas. Arbitrum One supports Cancun opcodes (ArbOS 20+), and the fork tests run them.
- *Profit measured as a balance difference* of the input token, not computed from the deltas.
  So it catches anything the deltas would miss.
- *Owner and factory are immutable.* There is no ownership transfer, which means less to get wrong.

**Assumptions.**
- Both pools are canonical Uniswap v3 pools from `factory` (0x1F98…F984, verified on-chain).
  Standard ERC-20s: no fee-on-transfer, no rebasing, no transfer hooks.
- The owner chooses `amountIn` and the direction off-chain (e.g. with `research/replay`'s
  exact sizing). The contract does not optimise size.
- `minProfit` is the only slippage guard. Set it to cover gas, or a profitable-but-below-gas
  trade will still go through.
- Swaps use no price limit (`MIN_SQRT_RATIO+1` / `MAX_SQRT_RATIO-1`). The protection is
  `minProfit`, not per-hop limits.

**Tests (fork of Arbitrum One at block 520884405, pinned in `foundry.toml`).**
- SYNTHETIC scenarios: a funded test account (`deal`) dumps 100 WETH into the 0.30% pool,
  which lowers its WETH price by about 1.5%. That opens a gap that does not exist at the
  pinned block. `execute(0.30%, 0.05%, USDC→WETH, 50,000 USDC)` then makes
  **519.940023 USDC** (gross, before gas). The figure only demonstrates the mechanics; it says
  nothing about real opportunity. Same setup with `minProfit = achievable + 1` reverts with
  `InsufficientProfit`. Without the dump, the same trade reverts with `CannotRepay`: at the
  pinned block the pools are within fees of each other.
- Callback from non-pool: an attacker EOA and a genuine but unauthorised factory pool both get
  `UnauthorizedCallback`, even when the executor holds stray funds. A fake contract that copies
  the 0.05% pool's `token0/token1/fee` gets `PoolNotFromFactory`.
- Other revert paths: not owner, zero/oversized amount, same pool, EOA as pool, pair mismatch
  (genuine WETH/USDC.e pool), zero owner. Rescue of tokens and ETH, owner-only.
- Fuzz (256 runs, fixed seed): dump 0–300 WETH (SYNTHETIC), either direction, `amountIn`
  1–1,000,000 USDC or 0.001–500 WETH, with `minProfit = 0`. The test asserts: either a
  revert, or the owner gains exactly `profit` >= 0 and the executor ends with zero of both
  tokens. On the same distribution about 25% of inputs succeed (all USDC-in), measured with
  a 200-draw sweep, so both branches are exercised.
- Parity: each of the 30 vectors in `tests/vectors/v3_swaps.json` is executed as a real
  swap on the forked pool. Amount out, amount in used, `sqrtPriceX96` after and tick after
  all match the Python port exactly, and the amount out also matches QuoterV2. A separate
  test checks that the vectors' block equals `foundry.toml`'s `fork_block_number`.
- Gas (`forge test --gas-report`): the SYNTHETIC successful `execute` costs 340,692 L2 gas.
  Over all calls it ranges from 22,839 (early revert) to 963,430 (large fuzz sizes crossing
  many ticks). Forge measures L2 execution gas only; Arbitrum's L1 data fee is not included.

**What can go wrong.**
- Fork gotchas found while writing the tests:
  - `block.number` on an Arbitrum fork is the **L1** block number (26334542 here), not the L2
    block, so it cannot be used to check the fork block.
  - `makeAddr("owner")` and `makeAddr("attacker")` already have contract code on Arbitrum One.
    One of them swallowed the rescued ETH. Tests now use unique labels and assert `code.length == 0`.
  - Bridged USDC.e's on-chain `symbol()` is `"USDC"`, not `"USDC.e"`.
- Sending the transaction publicly invites front-running and sandwiching: someone closes the
  gap first and the trade reverts (gas lost), or worse if `minProfit` is too low.
- Gas is not checked on-chain: a trade can be "profitable" in tokens and lose money net of gas.
- A pool whose token has transfer hooks could re-enter. The `_locked` flag and the single-use
  callback authorisation limit this, but non-standard tokens are out of scope.
- `deal` writes balances directly. USDC's FiatToken storage layout made this work here, but it
  is a test-only shortcut.

**Production-grade.**
- Private submission (e.g. a sequencer-side bundle/relay if available on Arbitrum) and
  `minProfit` derived from the gas price × gas estimate plus a margin.
- A deadline / max block number argument, so a delayed transaction cannot land stale.
- Multi-hop and multi-DEX routes (v3 forks with a different callback name, v2 pairs). Flash
  loans for non-cyclic routes.
- Two-step ownership and an executor allow-list, so a hot key can trade but cannot rescue
  funds to an arbitrary address.
- Fork tests at many blocks against real historical gaps from `episodes.parquet`, not only
  synthetic ones. Invariant tests with a handler that interleaves random swaps and executes.
- An independent audit, and Slither/forge lint clean.

### `research/forktests/run.py`: fork-test results with their config

**What it does.** Runs the Foundry suite three times on the pinned fork: statuses and logs,
the whole-suite gas report, and the gas of the single SYNTHETIC `execute`. It saves
`config.json`, `tests.json`, `gas_report.json` and `summary.md` under
`research/results/<run_id>/`.

**Why.** Forge prints results to a terminal and keeps nothing. The repo rule is that every
result is saved with its config and git commit. Every number in the summary is parsed from
forge's JSON, including the SYNTHETIC profit, which the test emits as a log line, so nothing is
retyped by hand. The parsing is pure functions, unit-tested in `tests/test_forktests_run.py`
without forge or RPC.

**Assumptions.** forge 1.9's `--json` layout (`test_results`, `kind.Unit/Fuzz`,
`decoded_logs` filled with `-vv`). `--gas-report --json` gives a list of
`{contract, functions{sig: {calls,min,mean,median,max}}}`. The profit log line format is fixed
by `contracts/test/V3ArbExecutor.t.sol`.

**What can go wrong.**
- A forge upgrade can change the JSON layout. The parser then raises (`KeyError`/`ValueError`)
  instead of saving wrong numbers.
- Forge's fork RPC cache (`~/.foundry/cache/rpc/`) makes reruns fast. A cold cache needs an
  archive RPC and takes minutes.
- Environment: the Python suite crashed on import (`yaml` segfault, pyarrow undefined symbol)
  because `.venv` had been created with CPython **3.14.0a5**, an alpha whose ABI differs from
  the 3.14 wheels in `uv.lock`. The fix was to update uv and install the final 3.14 (3.14.10),
  then rebuild with `uv venv --clear && uv sync --locked`. `.python-version` says `3.14`, so
  any 3.14.x final release works; pre-releases don't.

**Production-grade.** Run in CI on every change to `contracts/`, with the forge and solc
versions pinned in the CI image. Compare gas against the previous run and fail on regressions.
Add fork tests at the peak block of real historical episodes from `episodes.parquet`.
