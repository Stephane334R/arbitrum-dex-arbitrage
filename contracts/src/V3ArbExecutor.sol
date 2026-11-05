// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {IERC20Minimal, IUniswapV3Factory, IUniswapV3Pool} from "./interfaces/IUniswapV3.sol";

/// @title V3ArbExecutor
/// @notice Atomic two-pool arbitrage between Uniswap v3 pools of the same token pair.
///
/// Flow (flash style, no capital needed):
///   execute -> poolA.swap(exact input `amountIn` of tokenIn)
///     poolA sends tokenOut to us, then calls uniswapV3SwapCallback (stage A)
///       stage A -> poolB.swap(exact input = everything poolA sent, opposite direction)
///         poolB sends tokenIn to us, then calls uniswapV3SwapCallback (stage B)
///           stage B: pay poolB the tokenOut it is owed
///       stage A: pay poolA the tokenIn it is owed, out of what poolB sent
///   execute: gain = tokenIn balance after - before; revert if gain < minProfit;
///            send the gain to the owner, so the contract ends with what it started with.
///
/// The contract keeps no funds between trades: profit goes to the owner inside execute.
/// Anything sent to it by mistake can be recovered with rescueToken / rescueETH.
///
/// Not supported (documented, not handled): fee-on-transfer or rebasing tokens, pools that
/// are not from `factory`, routes with more than two pools.
contract V3ArbExecutor {
    // TickMath bounds (Uniswap v3 core). The swap price limit sits one unit inside them,
    // i.e. "no limit": the swap runs until amountIn is used or the pool runs out of liquidity.
    uint160 internal constant MIN_SQRT_RATIO = 4295128739;
    uint160 internal constant MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342;

    uint8 internal constant STAGE_A = 1;
    uint8 internal constant STAGE_B = 2;

    IUniswapV3Factory public immutable factory;
    address public immutable owner;

    /// Pool allowed to call uniswapV3SwapCallback next. Set only to a factory-verified pool,
    /// cleared by the callback itself (one callback per authorization). Transient storage
    /// (EIP-1153): it is reset at the end of every transaction even if something goes wrong.
    address internal transient _expectedPool;
    bool internal transient _locked;

    struct CallbackData {
        uint8 stage;
        bool zeroForOne; // direction of the swap that triggered this callback
        address tokenPay; // token this callback must pay to the pool
        address poolB; // stage A: the second pool; stage B: unused
        uint256 amountSpecified; // stage B: exact input we asked poolB to take; stage A: unused
    }

    event ArbExecuted(address indexed poolA, address indexed poolB, bool zeroForOne, uint256 amountIn, uint256 profit);
    event Rescued(address indexed token, address indexed to, uint256 amount);

    error NotOwner();
    error Reentrancy();
    error ZeroAmount();
    error AmountTooLarge(uint256 amountIn);
    error SamePool();
    error PoolNotFromFactory(address pool);
    error PairMismatch(address poolA, address poolB);
    error UnauthorizedCallback(address caller);
    error InvalidDeltas(int256 amount0Delta, int256 amount1Delta);
    error PartialFill(uint256 owed, uint256 expected);
    error CannotRepay(uint256 balance, uint256 owed);
    error Loss(uint256 balanceBefore, uint256 balanceAfter);
    error InsufficientProfit(uint256 profit, uint256 minProfit);
    error TransferFailed(address token, address to, uint256 amount);

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    error ZeroAddress();

    constructor(IUniswapV3Factory factory_, address owner_) {
        if (address(factory_) == address(0) || owner_ == address(0)) revert ZeroAddress();
        factory = factory_;
        owner = owner_;
    }

    /// @param poolA pool swapped first, exact input `amountIn` in direction `zeroForOne`
    /// @param poolB pool swapped second, opposite direction, exact input = poolA's output
    /// @param zeroForOne direction on poolA (true: token0 in, token1 out)
    /// @param amountIn exact input on poolA, in poolA's input token
    /// @param minProfit minimum gain in the input token, else the whole trade reverts
    /// @return profit gain in the input token, sent to the owner
    function execute(address poolA, address poolB, bool zeroForOne, uint256 amountIn, uint256 minProfit)
        external
        onlyOwner
        returns (uint256 profit)
    {
        if (_locked) revert Reentrancy();
        _locked = true;

        if (amountIn == 0) revert ZeroAmount();
        // forge-lint: disable-next-line(unsafe-typecast)
        if (amountIn > uint256(type(int256).max)) revert AmountTooLarge(amountIn);
        address tokenIn = _verifyRoute(poolA, poolB, zeroForOne);
        uint256 balanceBefore = IERC20Minimal(tokenIn).balanceOf(address(this));

        _expectedPool = poolA;
        _swap(poolA, zeroForOne, amountIn, CallbackData(STAGE_A, zeroForOne, tokenIn, poolB, 0));
        // A genuine pool always calls back (else it reverts 'IIA'); the callback clears this.
        if (_expectedPool != address(0)) revert UnauthorizedCallback(poolA);

        uint256 balanceAfter = IERC20Minimal(tokenIn).balanceOf(address(this));
        if (balanceAfter < balanceBefore) revert Loss(balanceBefore, balanceAfter);
        profit = balanceAfter - balanceBefore;
        if (profit < minProfit) revert InsufficientProfit(profit, minProfit);

        // tokenOut must be fully passed through: stage B reverts on a partial fill, so the
        // contract holds no tokenOut here. Profit leaves at once.
        if (profit > 0) _safeTransfer(tokenIn, owner, profit);
        emit ArbExecuted(poolA, poolB, zeroForOne, amountIn, profit);

        _locked = false;
    }

    /// @notice Called by a Uniswap v3 pool during swap(). Only the pool authorized in
    /// `_expectedPool` (factory-verified in execute) may call it, once.
    /// Sign convention (Uniswap v3): positive delta = we owe the pool, negative = we received.
    function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata data) external {
        address expected = _expectedPool;
        if (expected == address(0) || msg.sender != expected) revert UnauthorizedCallback(msg.sender);
        _expectedPool = address(0); // consume the authorization

        // `data` is what execute (or stage A) passed to swap(); a genuine pool forwards it as is,
        // and msg.sender was checked above, so it can be trusted.
        CallbackData memory cb = abi.decode(data, (CallbackData));

        (int256 owedDelta, int256 receivedDelta) =
            cb.zeroForOne ? (amount0Delta, amount1Delta) : (amount1Delta, amount0Delta);
        if (owedDelta <= 0 || receivedDelta > 0) revert InvalidDeltas(amount0Delta, amount1Delta);
        // casting to 'uint256' is safe because both values were checked to be >= 0 just above.
        // forge-lint: disable-next-line(unsafe-typecast)
        uint256 owed = uint256(owedDelta);
        // forge-lint: disable-next-line(unsafe-typecast)
        uint256 received = uint256(-receivedDelta);

        if (cb.stage == STAGE_A) {
            // We hold `received` of tokenOut. Sell all of it on poolB (opposite direction).
            bool zfoB = !cb.zeroForOne;
            address tokenOut = cb.zeroForOne ? IUniswapV3Pool(msg.sender).token1() : IUniswapV3Pool(msg.sender).token0();
            _expectedPool = cb.poolB;
            _swap(cb.poolB, zfoB, received, CallbackData(STAGE_B, zfoB, tokenOut, address(0), received));
            if (_expectedPool != address(0)) revert UnauthorizedCallback(cb.poolB);
            // Repay poolA in tokenIn out of poolB's output (plus any balance already held).
            uint256 balance = IERC20Minimal(cb.tokenPay).balanceOf(address(this));
            if (balance < owed) revert CannotRepay(balance, owed);
            _safeTransfer(cb.tokenPay, msg.sender, owed);
        } else {
            // Stage B: pay poolB the tokenOut it is owed. Exact input means owed == what we
            // asked to sell; anything else is a partial fill (price limit / liquidity exhausted)
            // that would leave tokenOut stuck here, so revert instead.
            if (owed != cb.amountSpecified) revert PartialFill(owed, cb.amountSpecified);
            _safeTransfer(cb.tokenPay, msg.sender, owed);
        }
    }

    function rescueToken(address token, address to, uint256 amount) external onlyOwner {
        _safeTransfer(token, to, amount);
        emit Rescued(token, to, amount);
    }

    function rescueETH(address payable to, uint256 amount) external onlyOwner {
        (bool ok,) = to.call{value: amount}("");
        if (!ok) revert TransferFailed(address(0), to, amount);
        emit Rescued(address(0), to, amount);
    }

    /// Both pools factory-verified, distinct, same (token0, token1). Returns poolA's input token.
    function _verifyRoute(address poolA, address poolB, bool zeroForOne) internal view returns (address tokenIn) {
        if (poolA == poolB) revert SamePool();
        (address token0, address token1) = _verifyPool(poolA);
        (address b0, address b1) = _verifyPool(poolB);
        if (b0 != token0 || b1 != token1) revert PairMismatch(poolA, poolB);
        tokenIn = zeroForOne ? token0 : token1;
    }

    /// Exact-input swap with no price limit; the callback receives `cb`.
    function _swap(address pool, bool zeroForOne, uint256 amountIn, CallbackData memory cb) internal {
        // casting to 'int256' is safe because execute bounds amountIn by type(int256).max and
        // stage B passes `received`, which came from a negated int256.
        // forge-lint: disable-next-line(unsafe-typecast)
        IUniswapV3Pool(pool).swap(address(this), zeroForOne, int256(amountIn), _priceLimit(zeroForOne), abi.encode(cb));
    }

    /// Reads the pool's own token0/token1/fee and checks factory.getPool returns this address.
    /// A contract that merely claims those tokens is rejected: the factory mapping is the
    /// source of truth.
    function _verifyPool(address pool) internal view returns (address token0, address token1) {
        if (pool.code.length == 0) revert PoolNotFromFactory(pool);
        token0 = IUniswapV3Pool(pool).token0();
        token1 = IUniswapV3Pool(pool).token1();
        uint24 fee = IUniswapV3Pool(pool).fee();
        if (factory.getPool(token0, token1, fee) != pool) revert PoolNotFromFactory(pool);
    }

    function _priceLimit(bool zeroForOne) internal pure returns (uint160) {
        return zeroForOne ? MIN_SQRT_RATIO + 1 : MAX_SQRT_RATIO - 1;
    }

    /// Works for tokens that return bool and for tokens that return nothing.
    function _safeTransfer(address token, address to, uint256 amount) internal {
        (bool ok, bytes memory ret) = token.call(abi.encodeCall(IERC20Minimal.transfer, (to, amount)));
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool))) || token.code.length == 0) {
            revert TransferFailed(token, to, amount);
        }
    }
}
