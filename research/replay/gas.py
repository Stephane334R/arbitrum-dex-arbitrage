"""Gas cost of a representative two-swap arbitrage transaction on Arbitrum.

Primary method: Arbitrum's NodeInterface virtual contract
(0x00000000000000000000000000000000000000C8, only reachable via eth_call/eth_estimateGas).
Its gasEstimateComponents(to, contractCreation, data) simulates a call and returns
(gasEstimate, gasEstimateForL1, baseFee, l1BaseFeeEstimate). gasEstimate is the total in L2
gas units, *including* the L1 data-posting component.

The simulated call is a round trip through the Uniswap SwapRouter02:
exactInput(path = WETH -fee_i-> USDC -fee_j-> WETH), paying with ETH (the router wraps it).
The sender's ETH balance comes from an eth_call state override, so no real funds are
involved. The router address is verified on-chain (code exists; factory() and WETH9() match
the registry) before use.

APPROXIMATION: a production arbitrage contract would call the pools directly and skip the
router's overhead (path decoding, WETH wrapping, callbacks via the router), so this estimate
probably *overstates* the gas a competitive arbitrageur pays. Gas also varies with the
number of ticks a swap crosses; this is one probe at one size.

Fallback: if NodeInterface fails, use `fallback_gas_units` from configs/replay.yaml. This is a
FIXED ESTIMATE and is labelled as such in config.json and summary.md.
"""

from __future__ import annotations

from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address

from data.rpc import RpcClient, RpcError

NODE_INTERFACE = "0x00000000000000000000000000000000000000C8"
_PROBE_SENDER = "0x000000000000000000000000000000000000dEaD"


def _sel(sig: str) -> bytes:
    return keccak(text=sig)[:4]


def _eth_call(client: RpcClient, to: str, data: bytes, block: int, **extra) -> bytes:
    params = [{"to": to, "data": "0x" + data.hex(), **extra.pop("tx", {})}, hex(block)]
    if "override" in extra:
        params.append(extra["override"])
    return bytes.fromhex(client.call("eth_call", params)[2:])


def verify_router(client: RpcClient, router: str, registry: dict, block: int) -> str:
    router = to_checksum_address(router)
    if len(client.call("eth_getCode", [router, hex(block)])) <= 2:
        raise RuntimeError(f"router {router}: no code")
    (factory,) = decode(["address"], _eth_call(client, router, _sel("factory()"), block))
    (weth9,) = decode(["address"], _eth_call(client, router, _sel("WETH9()"), block))
    if factory.lower() != registry["factory"]["address"].lower():
        raise RuntimeError(f"router {router}: factory() {factory} does not match registry")
    if weth9.lower() != registry["tokens"]["WETH"]["address"].lower():
        raise RuntimeError(f"router {router}: WETH9() {weth9} does not match registry")
    return router


def estimate_two_swap_gas(client: RpcClient, router: str, registry: dict, fee_i: int, fee_j: int,
                          block: int, amount_weth: float) -> dict:
    weth = bytes.fromhex(registry["tokens"]["WETH"]["address"][2:])
    usdc = bytes.fromhex(registry["tokens"]["USDC"]["address"][2:])
    path = weth + fee_i.to_bytes(3, "big") + usdc + fee_j.to_bytes(3, "big") + weth
    amount = int(amount_weth * 10**registry["tokens"]["WETH"]["decimals"])
    inner = _sel("exactInput((bytes,address,uint256,uint256))") + encode(
        ["(bytes,address,uint256,uint256)"], [(path, _PROBE_SENDER, amount, 0)]
    )
    data = _sel("gasEstimateComponents(address,bool,bytes)") + encode(["address", "bool", "bytes"], [router, False, inner])
    out = _eth_call(
        client, NODE_INTERFACE, data, block,
        tx={"from": _PROBE_SENDER, "value": hex(amount)},
        override={_PROBE_SENDER: {"balance": hex(amount * 10)}},
    )
    gas, gas_l1, base_fee, l1_base_fee = decode(["uint64", "uint64", "uint256", "uint256"], out)
    return {"gas_units": gas, "gas_units_l1": gas_l1, "base_fee_wei": base_fee, "l1_base_fee_estimate_wei": l1_base_fee}


def gas_for_pairs(client: RpcClient, cfg: dict, registry: dict, pairs: list[tuple[int, int]], block: int) -> dict:
    """Gas units per pair ("feeA-feeB"), plus a description of how they were obtained."""
    g = cfg["gas"]
    result = {"block": block, "per_pair": {}}
    try:
        router = verify_router(client, g["swap_router02"], registry, block)
        for fi, fj in pairs:
            result["per_pair"][f"{fi}-{fj}"] = estimate_two_swap_gas(client, router, registry, fi, fj, block, g["probe_amount_weth"])
        result |= {
            "method": "node_interface",
            "label": "NodeInterface.gasEstimateComponents on a SwapRouter02 two-swap round trip "
                     f"({g['probe_amount_weth']} WETH, state-overridden sender) at block {block}; "
                     "router overhead likely overstates a direct-pool arb contract's gas",
            "router": router,
        }
    except (RpcError, RuntimeError) as e:
        result |= {
            "method": "fixed",
            "label": f"FIXED ESTIMATE {g['fallback_gas_units']} gas units per arb (NodeInterface failed: {e})",
            "per_pair": {f"{fi}-{fj}": {"gas_units": g["fallback_gas_units"], "gas_units_l1": None} for fi, fj in pairs},
        }
    return result
