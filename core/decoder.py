"""
core/decoder.py — V7.0 Zero-RPC Calldata Decoder Engine

Local O(1) heuristic decoder for ERC-20 token transfers.
Cures "Token Blindness" without breaking rate limits.
"""
from typing import Optional
from core.types import Transaction, TokenTransfer

# Pre-compiled registry of top ERC-20 tokens to avoid API lookup
# Format: lower-cased contract address -> (symbol, decimals)
TOKEN_REGISTRY = {
    "0xdac17f958d2ee523a2206206994597c13d831ec7": ("USDT", 6),
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ("USDC", 6),
    "0x6b175474e89094c44da98b954eedeac495271d0f": ("DAI", 18),
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": ("WBTC", 8),
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": ("WETH", 18),
    "0x514910771af9ca656af840dff83e8264ecf986ca": ("LINK", 18),
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": ("UNI", 18),
    "0x95ad61b0a150d79219dcf64e1e6cc01f0b64c4ce": ("SHIB", 18),
    "0x6982508145454ce325ddbe47a25d4ec3d2311933": ("PEPE", 18),
    "0x7d1afa7b718fb893db30a3abc0cfc608aacfebb0": ("MATIC", 18),
    "0xae7ab96520de3a18e5e111b5eaab095312d7fe84": ("stETH", 18),
    "0x4e15361fd6b4bb609fa63c81a6be19d873717870": ("FTM", 18),
    "0x1a4b46696b2bb4794eb3d4c26f1c55f9170fa4c5": ("BIT", 18),
    "0xc18360217d8f7ab5e7c516566761ea12ce7f9d72": ("ENS", 18),
    "0x5a98fcbea516cf06857215779fd812ca3bef1b32": ("LDO", 18),
    "0xba100000625a3754423978a60c9317c58a424e3d": ("BAL", 18),
    "0x0d8775f648430679a709e98d2b0cb6250d2887ef": ("BAT", 18),
    "0xd533a949740bb3306d119cc777fa900ba034cd52": ("CRV", 18),
    "0x408e41876cccdc0f92210600ef50372656052a38": ("REN", 18),
    "0x0f5d2fb29fb7d3cfee444a200298f468908cc942": ("MANA", 18),
}

# 4-byte Keccak-256 signatures for standard ERC-20 transfers
TRANSFER_SIG = "0xa9059cbb"
TRANSFER_FROM_SIG = "0x23b872dd"

# Dummy price map for Phase I. In Phase II this will be updated via a shared memory block by the Pricing Daemon.
_DUMMY_PRICES = {
    "USDT": 1.0, "USDC": 1.0, "DAI": 1.0, "WBTC": 60000.0, "WETH": 3000.0, 
    "LINK": 15.0, "UNI": 10.0, "SHIB": 0.00002, "PEPE": 0.000008, "MATIC": 0.8,
    "stETH": 3000.0, "FTM": 0.5, "BIT": 1.0, "ENS": 20.0, "LDO": 2.5,
    "BAL": 4.0, "BAT": 0.25, "CRV": 0.5, "REN": 0.06, "MANA": 0.5
}

def fast_decode_erc20(tx: Transaction) -> Optional[TokenTransfer]:
    """
    O(1) local decoder for ERC-20 transfers.
    Extracts the recipient and amount using pure string slicing.
    Zero RPC calls.
    """
    input_data = tx.input_data
    if not input_data or len(input_data) < 138:
        return None

    # Target matching
    target_contract = tx.to_addr.lower()
    token_info = TOKEN_REGISTRY.get(target_contract)
    if not token_info:
        return None

    symbol, decimals = token_info
    sig = input_data[:10].lower()
    
    if sig == TRANSFER_SIG:
        if len(input_data) < 138:
            return None
        token_to = "0x" + input_data[34:74]
        amount_hex = input_data[74:138]
    elif sig == TRANSFER_FROM_SIG:
        if len(input_data) < 202:
            return None
        token_to = "0x" + input_data[98:138]
        amount_hex = input_data[138:202]
    else:
        return None

    try:
        amount_raw = int(amount_hex, 16)
    except ValueError:
        return None

    # Estimate USD amount locally to prevent breaking the StablecoinHunter bounds
    usd_price = _DUMMY_PRICES.get(symbol, 0.0)
    amount_usd = (amount_raw / (10 ** decimals)) * usd_price

    return TokenTransfer(
        source_tx=tx,
        contract=target_contract,
        symbol=symbol,
        token_to=token_to,
        amount_raw=amount_raw,
        decimals=decimals,
        amount_usd=amount_usd,
    )
