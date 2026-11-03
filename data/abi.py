"""Minimal ABIs (only the functions this project calls)."""


def _view(name: str, inputs: list[tuple[str, str]], outputs: list[tuple[str, str]]) -> dict:
    return {
        "type": "function",
        "name": name,
        "stateMutability": "view",
        "inputs": [{"name": n, "type": t} for n, t in inputs],
        "outputs": [{"name": n, "type": t} for n, t in outputs],
    }


ERC20 = [
    _view("symbol", [], [("", "string")]),
    _view("name", [], [("", "string")]),
    _view("decimals", [], [("", "uint8")]),
]

UNISWAP_V3_FACTORY = [
    _view("getPool", [("tokenA", "address"), ("tokenB", "address"), ("fee", "uint24")], [("", "address")]),
    _view("feeAmountTickSpacing", [("fee", "uint24")], [("", "int24")]),
]

UNISWAP_V3_POOL = [
    _view("factory", [], [("", "address")]),
    _view("token0", [], [("", "address")]),
    _view("token1", [], [("", "address")]),
    _view("fee", [], [("", "uint24")]),
    _view("tickSpacing", [], [("", "int24")]),
    _view("liquidity", [], [("", "uint128")]),
    _view(
        "slot0",
        [],
        [
            ("sqrtPriceX96", "uint160"),
            ("tick", "int24"),
            ("observationIndex", "uint16"),
            ("observationCardinality", "uint16"),
            ("observationCardinalityNext", "uint16"),
            ("feeProtocol", "uint8"),
            ("unlocked", "bool"),
        ],
    ),
]
