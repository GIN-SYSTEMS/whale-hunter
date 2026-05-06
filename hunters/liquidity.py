"""
hunters/liquidity.py — LiquiditySniper

Monitors pending Uniswap v2/v3 liquidity Mint and Burn transactions.
Flags massive LP events ($1M+ equivalent ETH) or LP operations from
wallets with fewer than 10 transactions (nonce < 10) — a sign of a
freshly deployed or newly funded liquidity actor.

Detection is purely selector-based against known Uniswap contract
addresses: sovereign-grade, no RPC calls, no oracle dependency.

Function selectors tracked:
  Mint (add liquidity):
    0xe8e33700  UniswapV2Router02.addLiquidity
    0xf305d719  UniswapV2Router02.addLiquidityETH
    0x88316456  NonfungiblePositionManager.mint  (v3)
  Burn (remove liquidity):
    0xbaa2abde  UniswapV2Router02.removeLiquidity
    0x02751cec  UniswapV2Router02.removeLiquidityETH
    0x0c49ccbe  NonfungiblePositionManager.decreaseLiquidity (v3)
"""

from __future__ import annotations

from typing import Optional

from core.types import Transaction, Signal, SignalCategory, HunterID
from core.config import Config
from .base import BaseHunter

# ── Uniswap contract addresses (Ethereum mainnet) ─────────
_UNI_CONTRACTS: frozenset[str] = frozenset({
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d",  # UniswapV2Router02
    "0xe592427a0aece92de3edee1f18e0157c05861564",  # UniswapV3Router
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45",  # UniswapV3Router02
    "0xc36442b4a4522e871399cd717abdd847ab11fe88",  # NonfungiblePositionManager
})

# ── Function selectors ────────────────────────────────────
_MINT_SELECTORS: frozenset[str] = frozenset({
    "0xe8e33700",  # addLiquidity(v2)
    "0xf305d719",  # addLiquidityETH(v2)
    "0x88316456",  # mint(v3 NonfungiblePositionManager)
})

_BURN_SELECTORS: frozenset[str] = frozenset({
    "0xbaa2abde",  # removeLiquidity(v2)
    "0x02751cec",  # removeLiquidityETH(v2)
    "0x0c49ccbe",  # decreaseLiquidity(v3)
})

# Wallets with nonce < _MAX_FRESH_NONCE are considered "fresh" actors
_MAX_FRESH_NONCE = 10
# ~$1M at $3000/ETH → 333 ETH whale threshold for ETH-based LP events
_ETH_WHALE_THRESHOLD = 333.0


class LiquiditySniper(BaseHunter):
    """Flags Uniswap v2/v3 Mint/Burn events from fresh wallets or whale actors."""

    hunter_id = HunterID.LIQUIDITY

    def evaluate(self, tx: Transaction) -> Optional[Signal]:
        if getattr(tx, 'value_eth', None) is None or getattr(tx, 'gas_price_gwei', None) is None:
            return None
        if not tx.input_data or len(tx.input_data) < 10:
            return None

        selector = tx.input_data[:10].lower()
        to_lower  = tx.to_addr.lower() if tx.to_addr else ""

        is_mint = selector in _MINT_SELECTORS
        is_burn = selector in _BURN_SELECTORS

        if not (is_mint or is_burn):
            return None
        if to_lower not in _UNI_CONTRACTS:
            return None

        is_fresh     = tx.nonce < _MAX_FRESH_NONCE
        is_whale_eth = tx.value_eth >= _ETH_WHALE_THRESHOLD

        # Must satisfy at least one criterion to produce a signal
        if not (is_fresh or is_whale_eth):
            return None

        event = "MINT" if is_mint else "BURN"
        score = 60

        if is_whale_eth:
            score += 20
        if is_fresh:
            score += 10
        if tx.nonce == 0:
            score += 5
        if is_burn:
            score += 5   # Removals (potential rug) are higher-priority
        score = min(score, 100)

        eth_price = getattr(self.config, "eth_price_usd", 3000.0)
        usd_est   = tx.value_eth * eth_price

        return Signal(
            category=SignalCategory.LIQUIDITY_MOVE,
            hunter_id=self.hunter_id,
            score=score,
            transaction=tx,
            label=f"LIQUIDITY {event}",
            details=(
                f"Uniswap {event} | Nonce: {tx.nonce} | "
                f"ETH: {tx.value_eth:,.1f} (~${usd_est:,.0f}) | "
                f"Contract: {to_lower[:16]}..."
            ),
        )
