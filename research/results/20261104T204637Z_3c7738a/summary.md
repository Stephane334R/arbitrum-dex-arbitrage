# QuoterV2 parity 20261104T204637Z_3c7738a

**Result.** 30/30 exact-input quotes match QuoterV2 (0x61fFE014bA17989E743c5F6cB21bF9697530B21e) exactly on amountOut, sqrtPriceX96After and initializedTicksCrossed, at Arbitrum One block 520884405.

| Vector | amountIn (raw) | amountOut (raw) | ticks crossed | loop steps | match |
|---|---:|---:|---:|---:|---|
| fee100_WETH_to_USDC_0.0001 | 100000000000000 | 227723 | 0 | 1 | yes |
| fee100_WETH_to_USDC_0.01 | 10000000000000000 | 22770595 | 0 | 1 | yes |
| fee100_WETH_to_USDC_0.3 | 300000000000000000 | 681554430 | 5 | 6 | yes |
| fee100_WETH_to_USDC_3 | 3000000000000000000 | 6680229232 | 28 | 31 | yes |
| fee100_WETH_to_USDC_10 | 10000000000000000000 | 20622976827 | 221 | 234 | yes |
| fee100_USDC_to_WETH_0.5 | 500000 | 219519781569486 | 0 | 1 | yes |
| fee100_USDC_to_WETH_30 | 30000000 | 13169837804809155 | 0 | 1 | yes |
| fee100_USDC_to_WETH_1000 | 1000000000 | 437521044871523173 | 0 | 1 | yes |
| fee100_USDC_to_WETH_10000 | 10000000000 | 4250365264393833326 | 35 | 39 | yes |
| fee100_USDC_to_WETH_30000 | 30000000000 | 12077484741600697604 | 138 | 146 | yes |
| fee500_WETH_to_USDC_0.0001 | 100000000000000 | 227686 | 0 | 1 | yes |
| fee500_WETH_to_USDC_0.1 | 100000000000000000 | 227685965 | 0 | 1 | yes |
| fee500_WETH_to_USDC_10 | 10000000000000000000 | 22765661094 | 1 | 2 | yes |
| fee500_WETH_to_USDC_300 | 300000000000000000000 | 680383289250 | 8 | 9 | yes |
| fee500_WETH_to_USDC_3000 | 3000000000000000000000 | 6511134687447 | 127 | 128 | yes |
| fee500_USDC_to_WETH_0.5 | 500000 | 219380878392496 | 0 | 1 | yes |
| fee500_USDC_to_WETH_300 | 300000000 | 131628300834359380 | 0 | 1 | yes |
| fee500_USDC_to_WETH_30000 | 30000000000 | 13160587339661109341 | 0 | 1 | yes |
| fee500_USDC_to_WETH_1000000 | 1000000000000 | 436233990098256094209 | 11 | 12 | yes |
| fee500_USDC_to_WETH_10000000 | 10000000000000 | 4140571246882761185571 | 120 | 121 | yes |
| fee3000_WETH_to_USDC_0.0001 | 100000000000000 | 226995 | 0 | 1 | yes |
| fee3000_WETH_to_USDC_0.1 | 100000000000000000 | 226994102 | 0 | 1 | yes |
| fee3000_WETH_to_USDC_10 | 10000000000000000000 | 22682288268 | 1 | 2 | yes |
| fee3000_WETH_to_USDC_300 | 300000000000000000000 | 665198366374 | 8 | 9 | yes |
| fee3000_WETH_to_USDC_3000 | 3000000000000000000000 | 2210978053862 | 211 | 221 | yes |
| fee3000_USDC_to_WETH_0.5 | 500000 | 218948735024720 | 0 | 1 | yes |
| fee3000_USDC_to_WETH_300 | 300000000 | 131367927951259233 | 0 | 1 | yes |
| fee3000_USDC_to_WETH_30000 | 30000000000 | 13123784787435277851 | 0 | 1 | yes |
| fee3000_USDC_to_WETH_1000000 | 1000000000000 | 423004076372629779759 | 13 | 14 | yes |
| fee3000_USDC_to_WETH_10000000 | 10000000000000 | 1202443089203204833285 | 232 | 242 | yes |

## Reproduce

- Config: `configs/v3_parity.yaml`; pools: `configs/pools.yaml`; RPC provider arb-mainnet.g.alchemy.com; git commit `3c7738a3afc9b8a603677df2509dec678a8182f6` (with uncommitted changes).
- `uv run python -m research.parity.quoter_parity`, then `uv run pytest tests/test_v3_swap_parity.py`.

## Scope

- Exact-input, single-pool quotes only. The exact-output path of the port is unit-tested (tests/test_v3_swap_math.py) but not checked against QuoterV2 here.
- One block, WETH/USDC pools only. Parity at one block is strong evidence, not proof, of parity in every state.
