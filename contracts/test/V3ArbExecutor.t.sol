// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {ForkBase} from "./utils/ForkBase.sol";
import {IUniswapV3Pool} from "../src/interfaces/IUniswapV3.sol";
import {V3ArbExecutor} from "../src/V3ArbExecutor.sol";

/// A contract that claims to be the WETH/USDC 0.05% pool (same token0/token1/fee) but is not
/// in the factory mapping. If the executor trusted it, swap() would call back into the
/// executor and try to take funds.
contract FakePool {
    address public immutable token0;
    address public immutable token1;
    uint24 public immutable fee;

    constructor(address t0, address t1, uint24 f) {
        token0 = t0;
        token1 = t1;
        fee = f;
    }

    function swap(address recipient, bool, int256 amountSpecified, uint160, bytes calldata data)
        external
        returns (int256, int256)
    {
        V3ArbExecutor(recipient).uniswapV3SwapCallback(amountSpecified, -1, data);
        return (amountSpecified, -1);
    }
}

/// Fork tests for V3ArbExecutor at the block pinned in foundry.toml.
///
/// SYNTHETIC scenarios: at the pinned block the WETH/USDC pools are within fees of each
/// other (no real arbitrage). A funded test account (deal) dumps WETH into the 0.30% pool to
/// open a gap. That dump is not a real market event; the profit numbers below only show
/// the contract's mechanics and are not an estimate of real opportunity.
contract V3ArbExecutorTest is ForkBase {
    // SYNTHETIC: 100 WETH sold into the 0.30% pool lowers its WETH price by ~1.5%
    // (sqrtPrice 3780427739765445981879965 -> 3751584142216414533660076 at the pinned block).
    uint256 internal constant DUMP_WETH = 100e18;
    // Arb size: buy WETH with 50,000 USDC on the cheap 0.30% pool, sell it on the 0.05% pool.
    uint256 internal constant ARB_USDC = 50_000e6;

    event ArbExecuted(address indexed poolA, address indexed poolB, bool zeroForOne, uint256 amountIn, uint256 profit);

    function _openSyntheticGap() internal {
        _dump(POOL_3000, true, DUMP_WETH);
    }

    function _execute(uint256 amountIn, uint256 minProfit) internal returns (uint256) {
        vm.prank(owner);
        return executor.execute(POOL_3000, POOL_500, false, amountIn, minProfit);
    }

    // ---------------------------------------------------------------- 1. SYNTHETIC success

    function test_SYNTHETIC_gapAfterLargeSwap_executeProfits() public {
        uint160 before = _sqrtPrice(POOL_3000);
        _openSyntheticGap();
        assertLt(_sqrtPrice(POOL_3000), before, "dump must lower the 0.30% pool's WETH price");

        uint256 ownerUsdc = _bal(USDC, owner);
        uint256 ownerWeth = _bal(WETH, owner);

        vm.expectEmit(true, true, false, false, address(executor));
        emit ArbExecuted(POOL_3000, POOL_500, false, ARB_USDC, 0); // profit not compared
        uint256 profit = _execute(ARB_USDC, 1e6); // at least 1 USDC

        assertGt(profit, 0, "profit");
        assertEq(_bal(USDC, owner) - ownerUsdc, profit, "owner receives exactly the profit");
        assertEq(_bal(WETH, owner), ownerWeth, "owner WETH untouched");
        // Holds no funds between trades.
        assertEq(_bal(USDC, address(executor)), 0, "executor USDC");
        assertEq(_bal(WETH, address(executor)), 0, "executor WETH");
        emit log_named_decimal_uint("SYNTHETIC profit (USDC)", profit, 6);
    }

    // ------------------------------------------------- 2. SYNTHETIC: minProfit not reached

    function test_SYNTHETIC_revertsWhenMinProfitAboveGain() public {
        _openSyntheticGap();
        // Measure the achievable profit, roll back, then demand one unit more.
        uint256 snap = vm.snapshotState();
        uint256 achievable = _execute(ARB_USDC, 0);
        vm.revertToState(snap);

        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(V3ArbExecutor.InsufficientProfit.selector, achievable, achievable + 1));
        executor.execute(POOL_3000, POOL_500, false, ARB_USDC, achievable + 1);
    }

    /// No synthetic dump: the pools are within fees of each other, so poolB's output cannot
    /// repay poolA and the whole trade reverts (nothing is lost, nothing is left behind).
    function test_noGap_revertsCannotRepay() public {
        vm.prank(owner);
        vm.expectPartialRevert(V3ArbExecutor.CannotRepay.selector);
        executor.execute(POOL_3000, POOL_500, false, ARB_USDC, 0);
    }

    // --------------------------------------------------- 3. callback from a non-pool address

    function test_callbackFromNonPool_reverts() public {
        // Nothing in flight: no pool is authorized.
        bytes memory data = abi.encode(V3ArbExecutor.CallbackData(2, true, USDC, address(0), 1));
        vm.prank(attacker);
        vm.expectRevert(abi.encodeWithSelector(V3ArbExecutor.UnauthorizedCallback.selector, attacker));
        executor.uniswapV3SwapCallback(1, -1, data);

        // Even a genuine factory pool cannot call it outside an execute it was authorized for.
        vm.prank(POOL_500);
        vm.expectRevert(abi.encodeWithSelector(V3ArbExecutor.UnauthorizedCallback.selector, POOL_500));
        executor.uniswapV3SwapCallback(1, -1, data);
    }

    function test_callbackFromNonPool_cannotDrainHeldFunds() public {
        // Even if funds were sent here by mistake, a direct callback cannot move them.
        deal(USDC, address(executor), 1_000e6);
        bytes memory data = abi.encode(V3ArbExecutor.CallbackData(2, true, USDC, address(0), 1_000e6));
        vm.prank(attacker);
        vm.expectRevert(abi.encodeWithSelector(V3ArbExecutor.UnauthorizedCallback.selector, attacker));
        executor.uniswapV3SwapCallback(int256(1_000e6), -1, data);
        assertEq(_bal(USDC, address(executor)), 1_000e6);
    }

    function test_fakePoolNotFromFactory_reverts() public {
        FakePool fake = new FakePool(WETH, USDC, 500); // claims to be the 0.05% pool
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(V3ArbExecutor.PoolNotFromFactory.selector, address(fake)));
        executor.execute(address(fake), POOL_500, true, 1e18, 0);

        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(V3ArbExecutor.PoolNotFromFactory.selector, address(fake)));
        executor.execute(POOL_500, address(fake), true, 1e18, 0);
    }

    // ------------------------------------------------------------ other revert paths

    function test_onlyOwner_execute() public {
        vm.prank(attacker);
        vm.expectRevert(V3ArbExecutor.NotOwner.selector);
        executor.execute(POOL_3000, POOL_500, false, ARB_USDC, 0);
    }

    function test_zeroAmount_reverts() public {
        vm.prank(owner);
        vm.expectRevert(V3ArbExecutor.ZeroAmount.selector);
        executor.execute(POOL_3000, POOL_500, false, 0, 0);
    }

    function test_amountTooLarge_reverts() public {
        uint256 tooLarge = uint256(type(int256).max) + 1;
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(V3ArbExecutor.AmountTooLarge.selector, tooLarge));
        executor.execute(POOL_3000, POOL_500, false, tooLarge, 0);
    }

    function test_samePool_reverts() public {
        vm.prank(owner);
        vm.expectRevert(V3ArbExecutor.SamePool.selector);
        executor.execute(POOL_500, POOL_500, true, 1e18, 0);
    }

    function test_eoaAsPool_reverts() public {
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(V3ArbExecutor.PoolNotFromFactory.selector, attacker));
        executor.execute(attacker, POOL_500, true, 1e18, 0);
    }

    function test_pairMismatch_reverts() public {
        // A genuine factory pool of another pair: WETH/USDC.e (bridged USDC). Its on-chain
        // symbol() is "USDC" (same as native USDC), so the name is checked instead. The pool
        // address is taken from factory.getPool at the fork block.
        address usdce = 0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8;
        assertEq(_str(usdce, "name()"), "USD Coin (Arb1)", "bridged USDC.e name");
        address other = FACTORY.getPool(WETH, usdce, 500);
        assertTrue(other != address(0), "WETH/USDC.e 0.05% pool exists");

        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(V3ArbExecutor.PairMismatch.selector, POOL_500, other));
        executor.execute(POOL_500, other, true, 1e18, 0);
    }

    function test_constructor_rejectsZeroAddresses() public {
        vm.expectRevert(V3ArbExecutor.ZeroAddress.selector);
        new V3ArbExecutor(FACTORY, address(0));
    }

    // ---------------------------------------------------------------------- rescue

    function test_rescueToken_ownerOnly() public {
        deal(WETH, address(executor), 3e18);

        vm.prank(attacker);
        vm.expectRevert(V3ArbExecutor.NotOwner.selector);
        executor.rescueToken(WETH, attacker, 3e18);

        vm.prank(owner);
        executor.rescueToken(WETH, owner, 3e18);
        assertEq(_bal(WETH, owner), 3e18);
        assertEq(_bal(WETH, address(executor)), 0);
    }

    function test_rescueETH_ownerOnly() public {
        vm.deal(address(executor), 1 ether); // e.g. forced in via selfdestruct

        vm.prank(attacker);
        vm.expectRevert(V3ArbExecutor.NotOwner.selector);
        executor.rescueETH(payable(attacker), 1 ether);

        vm.prank(owner);
        executor.rescueETH(payable(owner), 1 ether);
        assertEq(owner.balance, 1 ether);
    }

    // --------------------------------------------------------------- 4. fuzz: no silent loss

    /// For any gap size (0..300 WETH dumped, SYNTHETIC), either direction and any amountIn
    /// in a sensible range, with minProfit = 0 (the weakest guard): execute either reverts,
    /// or the owner gains exactly `profit` >= 0 of the input token, loses nothing of the
    /// other token, and the executor is left holding nothing.
    function testFuzz_neverLosesWithoutReverting(uint256 dumpWeth, bool zeroForOne, uint256 amountIn) public {
        dumpWeth = bound(dumpWeth, 0, 300e18);
        // WETH in: 0.001 .. 500 WETH. USDC in: 1 .. 1,000,000 USDC.
        amountIn = zeroForOne ? bound(amountIn, 1e15, 500e18) : bound(amountIn, 1e6, 1_000_000e6);
        if (dumpWeth > 0) _dump(POOL_3000, true, dumpWeth);

        (address tokenIn, address tokenOut) = zeroForOne ? (WETH, USDC) : (USDC, WETH);
        uint256 ownerIn = _bal(tokenIn, owner);
        uint256 ownerOut = _bal(tokenOut, owner);

        vm.prank(owner);
        try executor.execute(POOL_3000, POOL_500, zeroForOne, amountIn, 0) returns (uint256 profit) {
            assertEq(_bal(tokenIn, owner), ownerIn + profit, "owner gain == profit");
            assertEq(_bal(tokenOut, owner), ownerOut, "owner other token unchanged");
        } catch {
            // Reverted: no state change. Check the owner is untouched anyway.
            assertEq(_bal(tokenIn, owner), ownerIn);
            assertEq(_bal(tokenOut, owner), ownerOut);
        }
        assertEq(_bal(USDC, address(executor)), 0, "executor USDC");
        assertEq(_bal(WETH, address(executor)), 0, "executor WETH");
    }

    function _str(address token, string memory sig) internal view returns (string memory) {
        (bool ok, bytes memory ret) = token.staticcall(abi.encodeWithSignature(sig));
        require(ok, sig);
        return abi.decode(ret, (string));
    }
}
