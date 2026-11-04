"""Exact Python port of the Uniswap v3 swap math (v3-core 1.0.0, Solidity 0.7.6).

Ported line by line from the canonical sources (Uniswap/v3-core `contracts/libraries/` and
`UniswapV3Pool.swap`, plus v3-periphery `PoolTicksCounter`). It uses Python integers only, never
floats. The EVM's behaviour at the edges is emulated, not just the arithmetic:

- every `require` / overflow check that can make the Solidity code revert raises `SolidityRevert`;
- unchecked uint256 arithmetic (Solidity 0.7 does not check overflow) is reproduced where the
  original relies on it, e.g. the `product / amount == sqrtPX96` overflow test in
  `getNextSqrtPriceFromAmount0RoundingUp`;
- Solidity's integer division truncates toward zero, Python's `//` floors. Every signed
  division goes through `_sdiv` / `_smod`.

What this does *not* model (by design, none of it changes amounts or prices):
fee growth accounting, protocol fees, oracle observations, the callback/token transfers.

Tick data: `PoolSnapshot` holds the pool's `tickBitmap` words and `liquidityNet` for a *window*
of words around the current tick (see data/collectors/ticks.py). If a swap needs a word that is
outside the window, `swap()` raises `TickDataOutOfRange` instead of guessing. A result is
therefore either exact or an error, never a silent approximation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------------------------
# Constants and integer helpers
# ---------------------------------------------------------------------------------------------

MAX_UINT256 = 2**256 - 1
MAX_UINT160 = 2**160 - 1
MAX_UINT128 = 2**128 - 1
MAX_INT256 = 2**255 - 1
MIN_INT256 = -(2**255)
Q96 = 2**96

MIN_TICK = -887272
MAX_TICK = 887272
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342


class SolidityRevert(Exception):
    """The Solidity code would revert here (failed require, checked overflow, bad cast)."""


class TickDataOutOfRange(Exception):
    """The swap needs a tickBitmap word that the snapshot did not fetch. Fetch a wider window."""


def _require(cond: bool, reason: str = "") -> None:
    if not cond:
        raise SolidityRevert(reason)


def _sdiv(a: int, b: int) -> int:
    """Solidity signed division: truncates toward zero (Python `//` floors)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def _smod(a: int, b: int) -> int:
    """Solidity signed modulo: the result has the sign of the dividend."""
    return a - b * _sdiv(a, b)


def _to_uint160(x: int) -> int:  # SafeCast.toUint160
    _require(0 <= x <= MAX_UINT160, "toUint160")
    return x


def _to_int256(x: int) -> int:  # SafeCast.toInt256
    _require(x <= MAX_INT256, "toInt256")
    return x


# ---------------------------------------------------------------------------------------------
# FullMath / UnsafeMath / LiquidityMath
# ---------------------------------------------------------------------------------------------


def mul_div(a: int, b: int, denominator: int) -> int:
    """FullMath.mulDiv: floor(a*b/denominator) with a 512-bit intermediate product.
    Reverts if denominator == 0 or the result does not fit in uint256."""
    _require(denominator > 0, "mulDiv: denominator 0")
    result = (a * b) // denominator
    _require(result <= MAX_UINT256, "mulDiv: overflow")
    return result


def mul_div_rounding_up(a: int, b: int, denominator: int) -> int:
    """FullMath.mulDivRoundingUp: ceil(a*b/denominator), reverts if it does not fit in uint256."""
    result = mul_div(a, b, denominator)
    if (a * b) % denominator > 0:
        _require(result < MAX_UINT256, "mulDivRoundingUp: overflow")
        result += 1
    return result


def div_rounding_up(x: int, y: int) -> int:
    """UnsafeMath.divRoundingUp: ceil(x/y). EVM `div`/`mod` by zero return 0, so y == 0 gives 0
    (the callers never pass 0: liquidity and sqrt prices are checked first)."""
    if y == 0:
        return 0
    return x // y + (1 if x % y > 0 else 0)


def add_delta(x: int, y: int) -> int:
    """LiquidityMath.addDelta: uint128 + int128 with underflow ('LS') / overflow ('LA') checks."""
    if y < 0:
        z = (x - (-y)) % 2**128  # uint128 wraps in 0.7, then the require catches it
        _require(z < x, "LS")
    else:
        z = (x + y) % 2**128
        _require(z >= x, "LA")
    return z


# ---------------------------------------------------------------------------------------------
# TickMath
# ---------------------------------------------------------------------------------------------

_TICK_FACTORS = (
    (0x2, 0xFFF97272373D413259A46990580E213A),
    (0x4, 0xFFF2E50F5F656932EF12357CF3C7FDCC),
    (0x8, 0xFFE5CACA7E10E4E61C3624EAA0941CD0),
    (0x10, 0xFFCB9843D60F6159C9DB58835C926644),
    (0x20, 0xFF973B41FA98C081472E6896DFB254C0),
    (0x40, 0xFF2EA16466C96A3843EC78B326B52861),
    (0x80, 0xFE5DEE046A99A2A811C461F1969C3053),
    (0x100, 0xFCBE86C7900A88AEDCFFC83B479AA3A4),
    (0x200, 0xF987A7253AC413176F2B074CF7815E54),
    (0x400, 0xF3392B0822B70005940C7A398E4B70F3),
    (0x800, 0xE7159475A2C29B7443B29C7FA6E889D9),
    (0x1000, 0xD097F3BDFD2022B8845AD8F792AA5825),
    (0x2000, 0xA9F746462D870FDF8A65DC1F90E061E5),
    (0x4000, 0x70D869A156D2A1B890BB3DF62BAF32F7),
    (0x8000, 0x31BE135F97D08FD981231505542FCFA6),
    (0x10000, 0x9AA508B5B7A84E1C677DE54F3E99BC9),
    (0x20000, 0x5D6AF8DEDB81196699C329225EE604),
    (0x40000, 0x2216E584F5FA1EA926041BEDFE98),
    (0x80000, 0x48A170391F7DC42444E8FA2),
)


def get_sqrt_ratio_at_tick(tick: int) -> int:
    """TickMath.getSqrtRatioAtTick: sqrt(1.0001^tick) * 2^96, as the EVM computes it.

    1.0001^|tick| is built from the binary digits of |tick|: each set bit k multiplies a Q128.128
    accumulator by a precomputed 1/sqrt(1.0001)^(2^k) (rounded down, `>> 128`). For positive
    ticks the result is inverted (`MAX_UINT256 // ratio`). The final Q128.128 -> Q64.96 step
    rounds *up*, so getTickAtSqrtRatio(getSqrtRatioAtTick(t)) == t always holds."""
    abs_tick = -tick if tick < 0 else tick
    _require(abs_tick <= MAX_TICK, "T")
    ratio = 0xFFFCB933BD6FAD37AA2D162D1A594001 if abs_tick & 0x1 else 0x100000000000000000000000000000000
    for bit, factor in _TICK_FACTORS:
        if abs_tick & bit:
            ratio = (ratio * factor) >> 128
    if tick > 0:
        ratio = MAX_UINT256 // ratio
    return (ratio >> 32) + (0 if ratio % (1 << 32) == 0 else 1)


def get_tick_at_sqrt_ratio(sqrt_price_x96: int) -> int:
    """TickMath.getTickAtSqrtRatio: the greatest tick t with getSqrtRatioAtTick(t) <= price.

    Computes log2 of the Q128.128 ratio: the integer part from the most significant bit, then
    14 fractional bits by repeated squaring (each squaring doubles the log; if the square is
    >= 2, that fractional bit is 1). Multiplying by 1/log2(sqrt(1.0001)) gives log_sqrt(1.0001).
    That estimate carries a bounded error, so it yields two candidate ticks; if they differ,
    one exact getSqrtRatioAtTick call picks the right one."""
    _require(MIN_SQRT_RATIO <= sqrt_price_x96 < MAX_SQRT_RATIO, "R")
    ratio = sqrt_price_x96 << 32
    msb = ratio.bit_length() - 1  # same result as the assembly binary search
    r = ratio >> (msb - 127) if msb >= 128 else ratio << (127 - msb)
    log_2 = (msb - 128) << 64  # int256; Python's << and >> on negatives match the EVM's sar
    for shift in range(63, 49, -1):
        r = (r * r) >> 127
        f = r >> 128
        log_2 |= f << shift
        r >>= f
    log_sqrt10001 = log_2 * 255738958999603826347141  # Q128.128
    tick_low = (log_sqrt10001 - 3402992956809132418596140100660247210) >> 128
    tick_hi = (log_sqrt10001 + 291339464771989622907027621153398088495) >> 128
    if tick_low == tick_hi:
        return tick_low
    return tick_hi if get_sqrt_ratio_at_tick(tick_hi) <= sqrt_price_x96 else tick_low


# ---------------------------------------------------------------------------------------------
# SqrtPriceMath
# ---------------------------------------------------------------------------------------------


def get_next_sqrt_price_from_amount0_rounding_up(sqrt_px96: int, liquidity: int, amount: int, add: bool) -> int:
    """Price after adding (add=True) or removing `amount` of token0, rounded up.
    Formula: L * sqrtP / (L + amount * sqrtP), all in Q96 (numerator1 = L << 96)."""
    if amount == 0:
        return sqrt_px96
    numerator1 = liquidity << 96
    product = (amount * sqrt_px96) & MAX_UINT256  # unchecked uint256 multiply
    product_ok = product // amount == sqrt_px96  # the Solidity overflow test
    if add:
        if product_ok:
            denominator = (numerator1 + product) & MAX_UINT256  # unchecked add
            if denominator >= numerator1:
                return mul_div_rounding_up(numerator1, sqrt_px96, denominator)
        # Fallback when amount*sqrtP overflows: L / (L/sqrtP + amount), LowGasSafeMath add.
        d = numerator1 // sqrt_px96 + amount
        _require(d <= MAX_UINT256, "add overflow")
        return div_rounding_up(numerator1, d)
    _require(product_ok and numerator1 > product, "amount0 remove")
    denominator = numerator1 - product
    return _to_uint160(mul_div_rounding_up(numerator1, sqrt_px96, denominator))


def get_next_sqrt_price_from_amount1_rounding_down(sqrt_px96: int, liquidity: int, amount: int, add: bool) -> int:
    """Price after adding/removing `amount` of token1, rounded down: sqrtP +/- amount / L (Q96)."""
    if add:
        if amount <= MAX_UINT160:
            quotient = (amount << 96) // liquidity
        else:
            quotient = mul_div(amount, Q96, liquidity)
        s = sqrt_px96 + quotient
        _require(s <= MAX_UINT256, "add overflow")
        return _to_uint160(s)
    if amount <= MAX_UINT160:
        quotient = div_rounding_up(amount << 96, liquidity)
    else:
        quotient = mul_div_rounding_up(amount, Q96, liquidity)
    _require(sqrt_px96 > quotient, "amount1 remove")
    return sqrt_px96 - quotient


def get_next_sqrt_price_from_input(sqrt_px96: int, liquidity: int, amount_in: int, zero_for_one: bool) -> int:
    """Next price for an input amount; rounds so the price never overshoots the true target."""
    _require(sqrt_px96 > 0)
    _require(liquidity > 0)
    if zero_for_one:
        return get_next_sqrt_price_from_amount0_rounding_up(sqrt_px96, liquidity, amount_in, True)
    return get_next_sqrt_price_from_amount1_rounding_down(sqrt_px96, liquidity, amount_in, True)


def get_next_sqrt_price_from_output(sqrt_px96: int, liquidity: int, amount_out: int, zero_for_one: bool) -> int:
    """Next price for an output amount; rounds so the price always reaches the true target."""
    _require(sqrt_px96 > 0)
    _require(liquidity > 0)
    if zero_for_one:
        return get_next_sqrt_price_from_amount1_rounding_down(sqrt_px96, liquidity, amount_out, False)
    return get_next_sqrt_price_from_amount0_rounding_up(sqrt_px96, liquidity, amount_out, False)


def get_amount0_delta(sqrt_a: int, sqrt_b: int, liquidity: int, round_up: bool) -> int:
    """token0 between two prices: L * (sqrtB - sqrtA) / (sqrtA * sqrtB), in Q96.
    Rounded in two stages (first / sqrtB, then / sqrtA), exactly as on-chain. A single exact
    division can differ by 1 wei, so the two stages must be kept."""
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    numerator1 = liquidity << 96
    numerator2 = sqrt_b - sqrt_a
    _require(sqrt_a > 0)
    if round_up:
        return div_rounding_up(mul_div_rounding_up(numerator1, numerator2, sqrt_b), sqrt_a)
    return mul_div(numerator1, numerator2, sqrt_b) // sqrt_a


def get_amount1_delta(sqrt_a: int, sqrt_b: int, liquidity: int, round_up: bool) -> int:
    """token1 between two prices: L * (sqrtB - sqrtA) / 2^96."""
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    if round_up:
        return mul_div_rounding_up(liquidity, sqrt_b - sqrt_a, Q96)
    return mul_div(liquidity, sqrt_b - sqrt_a, Q96)


# ---------------------------------------------------------------------------------------------
# SwapMath
# ---------------------------------------------------------------------------------------------


def compute_swap_step(
    sqrt_ratio_current_x96: int,
    sqrt_ratio_target_x96: int,
    liquidity: int,
    amount_remaining: int,
    fee_pips: int,
) -> tuple[int, int, int, int]:
    """SwapMath.computeSwapStep: one step of a swap inside a single liquidity range.

    Moves the price from current toward target, using up at most `amount_remaining`
    (>= 0: exact input, gross of fee; < 0: exact output). Returns
    (sqrtRatioNextX96, amountIn, amountOut, feeAmount). The fee is taken from the input:
    the step can use at most amountRemaining * (1e6 - fee) / 1e6 for the swap itself."""
    zero_for_one = sqrt_ratio_current_x96 >= sqrt_ratio_target_x96
    exact_in = amount_remaining >= 0
    amount_in = amount_out = 0

    if exact_in:
        amount_remaining_less_fee = mul_div(amount_remaining, 10**6 - fee_pips, 10**6)
        amount_in = (
            get_amount0_delta(sqrt_ratio_target_x96, sqrt_ratio_current_x96, liquidity, True)
            if zero_for_one
            else get_amount1_delta(sqrt_ratio_current_x96, sqrt_ratio_target_x96, liquidity, True)
        )
        if amount_remaining_less_fee >= amount_in:
            sqrt_ratio_next_x96 = sqrt_ratio_target_x96
        else:
            sqrt_ratio_next_x96 = get_next_sqrt_price_from_input(
                sqrt_ratio_current_x96, liquidity, amount_remaining_less_fee, zero_for_one
            )
    else:
        amount_out = (
            get_amount1_delta(sqrt_ratio_target_x96, sqrt_ratio_current_x96, liquidity, False)
            if zero_for_one
            else get_amount0_delta(sqrt_ratio_current_x96, sqrt_ratio_target_x96, liquidity, False)
        )
        if -amount_remaining >= amount_out:
            sqrt_ratio_next_x96 = sqrt_ratio_target_x96
        else:
            sqrt_ratio_next_x96 = get_next_sqrt_price_from_output(
                sqrt_ratio_current_x96, liquidity, -amount_remaining, zero_for_one
            )

    is_max = sqrt_ratio_target_x96 == sqrt_ratio_next_x96

    if zero_for_one:
        if not (is_max and exact_in):
            amount_in = get_amount0_delta(sqrt_ratio_next_x96, sqrt_ratio_current_x96, liquidity, True)
        if not (is_max and not exact_in):
            amount_out = get_amount1_delta(sqrt_ratio_next_x96, sqrt_ratio_current_x96, liquidity, False)
    else:
        if not (is_max and exact_in):
            amount_in = get_amount1_delta(sqrt_ratio_current_x96, sqrt_ratio_next_x96, liquidity, True)
        if not (is_max and not exact_in):
            amount_out = get_amount0_delta(sqrt_ratio_current_x96, sqrt_ratio_next_x96, liquidity, False)

    if not exact_in and amount_out > -amount_remaining:
        amount_out = -amount_remaining

    if exact_in and sqrt_ratio_next_x96 != sqrt_ratio_target_x96:
        fee_amount = amount_remaining - amount_in  # didn't reach target: the rest is all fee
    else:
        fee_amount = mul_div_rounding_up(amount_in, fee_pips, 10**6 - fee_pips)

    return sqrt_ratio_next_x96, amount_in, amount_out, fee_amount


# ---------------------------------------------------------------------------------------------
# Pool snapshot + TickBitmap
# ---------------------------------------------------------------------------------------------


def compress(tick: int, tick_spacing: int) -> int:
    """tick / tickSpacing rounded toward negative infinity (as TickBitmap does)."""
    c = _sdiv(tick, tick_spacing)
    if tick < 0 and _smod(tick, tick_spacing) != 0:
        c -= 1
    return c


def position(compressed: int) -> tuple[int, int]:
    """TickBitmap.position: (wordPos = compressed >> 8, bitPos = compressed % 256 as uint8)."""
    return compressed >> 8, compressed & 0xFF


@dataclass
class PoolSnapshot:
    """Everything the swap loop reads from a pool, at one block.

    `bitmap` maps wordPos -> the 256-bit tickBitmap word, for every word in
    [word_lo, word_hi] (zero words included). `liquidity_net` holds liquidityNet for each
    initialized tick in those words."""

    sqrt_price_x96: int
    tick: int
    liquidity: int
    fee: int
    tick_spacing: int
    bitmap: dict[int, int]
    liquidity_net: dict[int, int]
    word_lo: int
    word_hi: int

    def word(self, word_pos: int) -> int:
        if not (self.word_lo <= word_pos <= self.word_hi):
            raise TickDataOutOfRange(
                f"tickBitmap word {word_pos} not fetched (have [{self.word_lo}, {self.word_hi}])"
            )
        return self.bitmap.get(word_pos, 0)

    def next_initialized_tick_within_one_word(self, tick: int, lte: bool) -> tuple[int, bool]:
        """TickBitmap.nextInitializedTickWithinOneWord. Looks for the next initialized tick at or
        below (lte) or above the current tick, but only within the current 256-bit word. If
        there is none, it returns the word boundary with initialized=False. The pool then takes
        a step to that boundary anyway: that is why the port needs the real word layout."""
        compressed = compress(tick, self.tick_spacing)
        if lte:
            word_pos, bit_pos = position(compressed)
            mask = (1 << bit_pos) - 1 + (1 << bit_pos)
            masked = self.word(word_pos) & mask
            initialized = masked != 0
            nxt = (compressed - (bit_pos - (masked.bit_length() - 1))) if initialized else (compressed - bit_pos)
        else:
            word_pos, bit_pos = position(compressed + 1)
            mask = ~((1 << bit_pos) - 1) & MAX_UINT256
            masked = self.word(word_pos) & mask
            initialized = masked != 0
            lsb = (masked & -masked).bit_length() - 1
            nxt = (compressed + 1 + (lsb - bit_pos)) if initialized else (compressed + 1 + (255 - bit_pos))
        return nxt * self.tick_spacing, initialized


def snapshot_from_positions(
    sqrt_price_x96: int, fee: int, tick_spacing: int, positions: list[tuple[int, int, int]], word_range: int = 4
) -> PoolSnapshot:
    """Build a synthetic snapshot from (tickLower, tickUpper, liquidity) positions. For tests.
    Active liquidity is the sum over positions with tickLower <= tick < tickUpper."""
    tick = get_tick_at_sqrt_ratio(sqrt_price_x96)
    net: dict[int, int] = {}
    for lo, hi, liq in positions:
        if not (lo < hi and lo % tick_spacing == 0 and hi % tick_spacing == 0):
            raise ValueError(f"bad position ({lo}, {hi}) for tickSpacing {tick_spacing}")
        net[lo] = net.get(lo, 0) + liq
        net[hi] = net.get(hi, 0) - liq
    net = {t: n for t, n in net.items() if n != 0}  # simplification: gross==0 <=> uninitialized
    bitmap: dict[int, int] = {}
    for t in net:
        w, b = position(compress(t, tick_spacing))
        bitmap[w] = bitmap.get(w, 0) | (1 << b)
    active = sum(liq for lo, hi, liq in positions if lo <= tick < hi)
    w0, _ = position(compress(tick, tick_spacing))
    return PoolSnapshot(
        sqrt_price_x96, tick, active, fee, tick_spacing, bitmap, net, w0 - word_range, w0 + word_range
    )


# ---------------------------------------------------------------------------------------------
# UniswapV3Pool.swap
# ---------------------------------------------------------------------------------------------


@dataclass
class SwapResult:
    amount0: int  # pool's view: > 0 the pool receives token0, < 0 it pays out token0
    amount1: int
    amount_in: int  # input actually consumed, fee included (< requested if the limit was hit)
    amount_out: int
    fee_amount: int  # total LP + protocol fee, in the input token
    sqrt_price_x96_after: int
    tick_after: int
    liquidity_after: int
    ticks_crossed: list[int] = field(default_factory=list)  # initialized ticks actually crossed
    steps: int = 0  # loop iterations, word-boundary steps included


def swap(
    pool: PoolSnapshot,
    zero_for_one: bool,
    amount_specified: int,
    sqrt_price_limit_x96: int | None = None,
) -> SwapResult:
    """UniswapV3Pool.swap without the side effects. amount_specified > 0: exact input,
    < 0: exact output. sqrt_price_limit_x96=None uses the QuoterV2 default (MIN+1 / MAX-1).
    The pool snapshot is not modified."""
    _require(amount_specified != 0, "AS")
    if sqrt_price_limit_x96 is None:
        sqrt_price_limit_x96 = MIN_SQRT_RATIO + 1 if zero_for_one else MAX_SQRT_RATIO - 1
    if zero_for_one:
        _require(MIN_SQRT_RATIO < sqrt_price_limit_x96 < pool.sqrt_price_x96, "SPL")
    else:
        _require(pool.sqrt_price_x96 < sqrt_price_limit_x96 < MAX_SQRT_RATIO, "SPL")

    exact_input = amount_specified > 0
    remaining = amount_specified
    calculated = 0
    sqrt_price = pool.sqrt_price_x96
    tick = pool.tick
    liquidity = pool.liquidity
    fee_total = 0
    crossed: list[int] = []
    steps = 0

    while remaining != 0 and sqrt_price != sqrt_price_limit_x96:
        steps += 1
        sqrt_start = sqrt_price
        tick_next, initialized = pool.next_initialized_tick_within_one_word(tick, zero_for_one)
        tick_next = max(MIN_TICK, min(MAX_TICK, tick_next))
        sqrt_next = get_sqrt_ratio_at_tick(tick_next)
        beyond_limit = sqrt_next < sqrt_price_limit_x96 if zero_for_one else sqrt_next > sqrt_price_limit_x96
        target = sqrt_price_limit_x96 if beyond_limit else sqrt_next

        sqrt_price, step_in, step_out, step_fee = compute_swap_step(sqrt_price, target, liquidity, remaining, pool.fee)
        fee_total += step_fee

        if exact_input:
            remaining -= _to_int256(step_in + step_fee)
            calculated -= _to_int256(step_out)
        else:
            remaining += _to_int256(step_out)
            calculated += _to_int256(step_in + step_fee)
        _require(MIN_INT256 <= calculated <= MAX_INT256, "SignedSafeMath")

        if sqrt_price == sqrt_next:
            if initialized:
                if tick_next not in pool.liquidity_net:
                    raise TickDataOutOfRange(f"bitmap marks tick {tick_next} initialized but liquidityNet missing")
                net = pool.liquidity_net[tick_next]
                if zero_for_one:
                    net = -net
                liquidity = add_delta(liquidity, net)
                crossed.append(tick_next)
            tick = tick_next - 1 if zero_for_one else tick_next
        elif sqrt_price != sqrt_start:
            tick = get_tick_at_sqrt_ratio(sqrt_price)

    if zero_for_one == exact_input:
        amount0, amount1 = amount_specified - remaining, calculated
    else:
        amount0, amount1 = calculated, amount_specified - remaining

    a_in, a_out = (amount0, -amount1) if zero_for_one else (amount1, -amount0)
    return SwapResult(
        amount0=amount0,
        amount1=amount1,
        amount_in=a_in,
        amount_out=a_out,
        fee_amount=fee_total,
        sqrt_price_x96_after=sqrt_price,
        tick_after=tick,
        liquidity_after=liquidity,
        ticks_crossed=crossed,
        steps=steps,
    )


def quote_exact_input_single(pool: PoolSnapshot, zero_for_one: bool, amount_in: int) -> SwapResult:
    """What QuoterV2.quoteExactInputSingle(sqrtPriceLimitX96=0) simulates. QuoterV2 rejects a
    swap that moves no tokens at all (its callback requires a positive delta); so does this."""
    res = swap(pool, zero_for_one, amount_in)
    _require(res.amount0 > 0 or res.amount1 > 0, "QuoterV2: no liquidity")
    return res


# ---------------------------------------------------------------------------------------------
# v3-periphery PoolTicksCounter (QuoterV2's `initializedTicksCrossed`)
# ---------------------------------------------------------------------------------------------


def count_initialized_ticks_crossed(pool: PoolSnapshot, tick_before: int, tick_after: int) -> int:
    """Port of PoolTicksCounter.countInitializedTicksCrossed, used by QuoterV2.

    This is *not* the number of ticks the swap loop crossed. It counts the initialized ticks in
    the bitmap between tickBefore and tickAfter (inclusive), then removes an endpoint that is
    itself initialized when the swap did not cross it. Note that it divides by tickSpacing with
    Solidity truncation, not floor, unlike TickBitmap. Ported only to reproduce QuoterV2's
    output; use SwapResult.ticks_crossed for the real count."""
    s = pool.tick_spacing

    def pos(t: int) -> tuple[int, int]:
        c = _sdiv(t, s)
        return c >> 8, c & 0xFF

    word_pos, bit_pos = pos(tick_before)
    word_pos_after, bit_pos_after = pos(tick_after)
    after_init = (pool.word(word_pos_after) >> bit_pos_after) & 1 and _smod(tick_after, s) == 0 and tick_before > tick_after
    before_init = (pool.word(word_pos) >> bit_pos) & 1 and _smod(tick_before, s) == 0 and tick_before < tick_after

    if word_pos < word_pos_after or (word_pos == word_pos_after and bit_pos <= bit_pos_after):
        wl, bl, wh, bh = word_pos, bit_pos, word_pos_after, bit_pos_after
    else:
        wl, bl, wh, bh = word_pos_after, bit_pos_after, word_pos, bit_pos

    count = 0
    mask = (MAX_UINT256 << bl) & MAX_UINT256
    while wl <= wh:
        if wl == wh:
            mask &= MAX_UINT256 >> (255 - bh)
        count += bin(pool.word(wl) & mask).count("1")
        wl += 1
        mask = MAX_UINT256
    if after_init:
        count -= 1
    if before_init:
        count -= 1
    return count % 2**32  # uint32 (0.7 unchecked)
