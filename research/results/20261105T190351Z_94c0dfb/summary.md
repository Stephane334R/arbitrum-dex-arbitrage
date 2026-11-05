# Foundry fork tests 20261105T190351Z_94c0dfb

**Result.** 18/18 tests pass on an Arbitrum One fork at block 520884405. SYNTHETIC scenario: a test account dumps 100 WETH into the 0.30% pool, then `execute` (0.30% -> 0.05%, 50,000 USDC in) makes **519.940023 USDC** gross, using 340,692 L2 gas.

The gap is SYNTHETIC: it does not exist at the pinned block (the same trade without the dump reverts with `CannotRepay`). The profit shows the contract works; it is not an estimate of real opportunity. Gas is forge's L2 execution gas; Arbitrum's L1 data fee is not included.

| Suite | Test | Status | Gas |
|---|---|---|---:|
| V3ArbExecutorTest | `testFuzz_neverLosesWithoutReverting(uint256,bool,uint256)` | Success | 256 runs, median 1,277,137 |
| V3ArbExecutorTest | `test_SYNTHETIC_gapAfterLargeSwap_executeProfits()` | Success | 885,004 |
| V3ArbExecutorTest | `test_SYNTHETIC_revertsWhenMinProfitAboveGain()` | Success | 1,098,707 |
| V3ArbExecutorTest | `test_amountTooLarge_reverts()` | Success | 34,244 |
| V3ArbExecutorTest | `test_callbackFromNonPool_cannotDrainHeldFunds()` | Success | 294,996 |
| V3ArbExecutorTest | `test_callbackFromNonPool_reverts()` | Success | 60,419 |
| V3ArbExecutorTest | `test_constructor_rejectsZeroAddresses()` | Success | 173,355 |
| V3ArbExecutorTest | `test_eoaAsPool_reverts()` | Success | 39,188 |
| V3ArbExecutorTest | `test_fakePoolNotFromFactory_reverts()` | Success | 357,901 |
| V3ArbExecutorTest | `test_noGap_revertsCannotRepay()` | Success | 279,800 |
| V3ArbExecutorTest | `test_onlyOwner_execute()` | Success | 33,428 |
| V3ArbExecutorTest | `test_pairMismatch_reverts()` | Success | 76,336 |
| V3ArbExecutorTest | `test_rescueETH_ownerOnly()` | Success | 64,709 |
| V3ArbExecutorTest | `test_rescueToken_ownerOnly()` | Success | 370,968 |
| V3ArbExecutorTest | `test_samePool_reverts()` | Success | 33,972 |
| V3ArbExecutorTest | `test_zeroAmount_reverts()` | Success | 33,755 |
| V3SwapParityTest | `test_onChainSwapsMatchPythonVectors()` | Success | 58,732,726 |
| V3SwapParityTest | `test_vectorsAreFromThePinnedBlock()` | Success | 2,544,868 |

Test gas includes setup inside the test (e.g. the SYNTHETIC dump), not only `execute`.

## `execute` gas

- SYNTHETIC success, alone: 340,692.
- All 269 calls in the suite (reverts and large fuzz sizes included): min 22,839, median 704,361, max 963,430.

## Reproduce

- git commit `94c0dfb6a469764b50b1c9bf838cd8c6e7d7a703`; forge Version: 1.9.0; solc 0.8.28; forge-std `f3dae6e6ee381f25eb6a246f7da9b85c91a68219`; RPC provider arb-mainnet.g.alchemy.com.
- Fuzz: 256 runs, seed `0x2a` (from contracts/foundry.toml).
- `uv run python -m research.forktests.run` (or `cd contracts && forge test -vv`).
