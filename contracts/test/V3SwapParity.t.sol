// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {ForkBase} from "./utils/ForkBase.sol";
import {IUniswapV3Pool} from "../src/interfaces/IUniswapV3.sol";

/// Parity: the Python port (data/pricing/v3_swap.py) predicted each exact-input swap in
/// tests/vectors/v3_swaps.json, and QuoterV2 agreed (30/30). Here each swap is executed for
/// real against the forked pool, from the same block, and the results must match exactly:
/// amount out, amount in used, sqrtPriceX96 and tick after the swap.
/// Each vector starts from the pinned-block state (snapshot / revert between vectors).
contract V3SwapParityTest is ForkBase {
    string internal constant VECTORS = "../tests/vectors/v3_swaps.json";

    function test_vectorsAreFromThePinnedBlock() public view {
        string memory json = vm.readFile(VECTORS);
        string memory toml = vm.readFile("foundry.toml");
        uint256 vectorsBlock = vm.parseJsonUint(json, ".meta.block");
        uint256 pinned = vm.parseTomlUint(toml, ".profile.default.fork_block_number");
        assertEq(vectorsBlock, pinned, "vectors block != foundry.toml fork_block_number");
        assertEq(pinned, FORK_BLOCK, "ForkBase.FORK_BLOCK != foundry.toml");
        assertEq(vm.parseJsonUint(json, ".meta.chain_id"), block.chainid, "chain id");
    }

    function test_onChainSwapsMatchPythonVectors() public {
        string memory json = vm.readFile(VECTORS);
        uint256 n;
        while (vm.keyExistsJson(json, string.concat(".vectors[", vm.toString(n), "]"))) n++;
        assertEq(n, 30, "expected 30 vectors");

        for (uint256 i; i < n; i++) {
            uint256 snap = vm.snapshotState();
            _checkVector(json, string.concat(".vectors[", vm.toString(i), "]"));
            vm.revertToState(snap);
        }
    }

    struct Vector {
        string id;
        address pool;
        uint24 fee;
        bool zeroForOne;
        uint256 amountIn;
        uint256 pyOut;
        uint256 pyInUsed;
        uint256 pySqrt;
        int256 pyTick;
        uint256 quoterOut;
    }

    function _load(string memory json, string memory k) internal pure returns (Vector memory v) {
        v.id = vm.parseJsonString(json, string.concat(k, ".id"));
        v.pool = vm.parseJsonAddress(json, string.concat(k, ".pool"));
        v.fee = uint24(vm.parseJsonUint(json, string.concat(k, ".fee")));
        v.zeroForOne = vm.parseJsonBool(json, string.concat(k, ".zero_for_one"));
        v.amountIn = vm.parseJsonUint(json, string.concat(k, ".amount_in"));
        v.pyOut = vm.parseJsonUint(json, string.concat(k, ".python.amount_out"));
        v.pyInUsed = vm.parseJsonUint(json, string.concat(k, ".python.amount_in_consumed"));
        v.pySqrt = vm.parseJsonUint(json, string.concat(k, ".python.sqrt_price_x96_after"));
        v.pyTick = vm.parseJsonInt(json, string.concat(k, ".python.tick_after"));
        v.quoterOut = vm.parseJsonUint(json, string.concat(k, ".quoter.amount_out"));
    }

    function _checkVector(string memory json, string memory k) internal {
        Vector memory v = _load(json, k);

        // The vector's pool must be the verified factory pool for its fee tier.
        assertEq(FACTORY.getPool(WETH, USDC, v.fee), v.pool, string.concat(v.id, ": pool not from factory"));

        deal(v.zeroForOne ? WETH : USDC, address(swapper), v.amountIn);
        (uint256 out, uint256 inUsed) = swapper.swapExactIn(v.pool, v.zeroForOne, v.amountIn);
        (uint160 sqrtAfter, int24 tickAfter,,,,,) = IUniswapV3Pool(v.pool).slot0();

        assertEq(out, v.pyOut, string.concat(v.id, ": amount_out (on-chain vs python)"));
        assertEq(out, v.quoterOut, string.concat(v.id, ": amount_out (on-chain vs quoter)"));
        assertEq(inUsed, v.pyInUsed, string.concat(v.id, ": amount_in_consumed"));
        assertEq(uint256(sqrtAfter), v.pySqrt, string.concat(v.id, ": sqrt_price_x96_after"));
        assertEq(int256(tickAfter), v.pyTick, string.concat(v.id, ": tick_after"));
    }
}
