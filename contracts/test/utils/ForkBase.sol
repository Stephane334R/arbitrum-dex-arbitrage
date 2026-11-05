// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Test} from "forge-std/Test.sol";
import {IERC20Minimal, IUniswapV3Factory, IUniswapV3Pool} from "../../src/interfaces/IUniswapV3.sol";
import {V3ArbExecutor} from "../../src/V3ArbExecutor.sol";

/// Swaps directly against a pool through the callback, like a router would, but with no
/// slippage checks. Used to (a) move a pool's price in SYNTHETIC scenarios and (b) run the
/// real pool swap for the parity test. Test-only.
contract PoolSwapper {
    address private _pool;

    function swapExactIn(address pool, bool zeroForOne, uint256 amountIn)
        external
        returns (uint256 amountOut, uint256 amountInUsed)
    {
        _pool = pool;
        (int256 a0, int256 a1) = IUniswapV3Pool(pool)
            .swap(
                address(this),
                zeroForOne,
                int256(amountIn),
                zeroForOne ? uint160(4295128739 + 1) : uint160(1461446703485210103287273052203988822378723970342 - 1),
                ""
            );
        _pool = address(0);
        (int256 inDelta, int256 outDelta) = zeroForOne ? (a0, a1) : (a1, a0);
        amountInUsed = uint256(inDelta);
        amountOut = uint256(-outDelta);
    }

    function uniswapV3SwapCallback(int256 a0, int256 a1, bytes calldata) external {
        require(msg.sender == _pool, "PoolSwapper: bad caller");
        IUniswapV3Pool p = IUniswapV3Pool(msg.sender);
        if (a0 > 0) IERC20Minimal(p.token0()).transfer(msg.sender, uint256(a0));
        if (a1 > 0) IERC20Minimal(p.token1()).transfer(msg.sender, uint256(a1));
    }
}

/// Shared fork setup. The fork itself (RPC alias + pinned block) comes from foundry.toml.
/// Every address below was read back from the chain at the pinned block (factory.getPool,
/// fee(), tickSpacing(), symbol(), decimals()) and matches configs/pools.yaml; setUp
/// re-checks the parts the tests depend on, so a wrong constant fails loudly.
abstract contract ForkBase is Test {
    uint256 internal constant FORK_BLOCK = 520884405;

    IUniswapV3Factory internal constant FACTORY = IUniswapV3Factory(0x1F98431c8aD98523631AE4a59f267346ea31F984);
    address internal constant WETH = 0x82aF49447D8a07e3bd95BD0d56f35241523fBab1; // token0, 18 dp
    address internal constant USDC = 0xaf88d065e77c8cC2239327C5EDb3A432268e5831; // token1, 6 dp

    address internal constant POOL_100 = 0x6f38e884725a116C9C7fBF208e79FE8828a2595F; // 0.01%
    address internal constant POOL_500 = 0xC6962004f452bE9203591991D15f6b388e09E8D0; // 0.05%
    address internal constant POOL_3000 = 0xc473e2aEE3441BF9240Be85eb122aBB059A3B57c; // 0.30%

    // Unique labels: makeAddr("owner") and makeAddr("attacker") are common in Foundry tests
    // and both already have contract code on Arbitrum One. setUp asserts these have none.
    address internal owner = makeAddr("v3-arb-executor-test-owner");
    address internal attacker = makeAddr("v3-arb-executor-test-attacker");
    V3ArbExecutor internal executor;
    PoolSwapper internal swapper;

    function setUp() public virtual {
        // foundry.toml pins the fork; make sure that is what we are running on.
        assertEq(block.chainid, 42161, "not an Arbitrum One fork");
        assertEq(owner.code.length, 0, "owner must be an EOA on the fork");
        assertEq(attacker.code.length, 0, "attacker must be an EOA on the fork");
        _checkPool(POOL_100, 100);
        _checkPool(POOL_500, 500);
        _checkPool(POOL_3000, 3000);

        executor = new V3ArbExecutor(FACTORY, owner);
        swapper = new PoolSwapper();
    }

    function _checkPool(address pool, uint24 fee) internal view {
        assertEq(FACTORY.getPool(WETH, USDC, fee), pool, "factory.getPool mismatch");
        assertEq(IUniswapV3Pool(pool).token0(), WETH, "token0");
        assertEq(IUniswapV3Pool(pool).token1(), USDC, "token1");
        assertEq(IUniswapV3Pool(pool).fee(), fee, "fee");
    }

    /// Funds the swapper with `amountIn` of the input token (deal: storage write, no mint
    /// event, totalSupply unchanged) and swaps it on `pool`.
    function _dump(address pool, bool zeroForOne, uint256 amountIn) internal returns (uint256 amountOut) {
        deal(zeroForOne ? WETH : USDC, address(swapper), amountIn);
        (amountOut,) = swapper.swapExactIn(pool, zeroForOne, amountIn);
    }

    function _sqrtPrice(address pool) internal view returns (uint160 sqrtPriceX96) {
        (sqrtPriceX96,,,,,,) = IUniswapV3Pool(pool).slot0();
    }

    function _bal(address token, address who) internal view returns (uint256) {
        return IERC20Minimal(token).balanceOf(who);
    }
}
