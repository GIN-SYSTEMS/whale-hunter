"""
hunters/stablecoin.py — StablecoinHunter

Monitors large ERC-20 transfers (USDT, USDC, DAI, WBTC, LINK) decoded
from pending transaction input_data. Operates on TokenTransfer objects
rather than raw Transaction objects (separate evaluation path in main.py).

Tiers:
  >= $500k  → DOLPHIN move  (score 55)
  >= $1M    → WHALE move    (score 75)
  >= $10M   → MEGA WHALE    (score 92)
"""

from __future__ import annotations

from typing import Optional

from core.types import TokenTransfer, Signal, SignalCategory, HunterID
from core.config import Config

_TIER_DOLPHIN  = 500_000     # $500k
_TIER_WHALE    = 1_000_000   # $1M
_TIER_MEGA     = 10_000_000  # $10M


class StablecoinHunter:
    """Evaluates TokenTransfer objects for stablecoin / high-cap whale activity."""

    hunter_id = HunterID.STABLECOIN

    def __init__(self, config: Config):
        self.config = config

    def evaluate_token(self, tt: TokenTransfer) -> Optional[Signal]:
        if getattr(tt, 'amount_usd', None) is None or getattr(tt, 'amount_raw', None) is None:
            return None
        if tt.amount_usd < _TIER_DOLPHIN:
            return None

        if tt.amount_usd >= _TIER_MEGA:
            score = 92
            tier  = "MEGA WHALE"
        elif tt.amount_usd >= _TIER_WHALE:
            score = 75
            tier  = "WHALE"
        else:
            score = 55
            tier  = "DOLPHIN"

        return Signal(
            category=SignalCategory.TOKEN_TRANSFER,
            hunter_id=self.hunter_id,
            score=score,
            transaction=tt.source_tx,
            label=f"STABLECOIN {tier} | {tt.symbol}",
            details=(
                f"{tt.symbol}: {tt.amount_human:,.2f} "
                f"(${tt.amount_usd:,.0f}) | "
                f"Contract: {tt.contract[:14]}... | "
                f"Recipient: {tt.token_to[:14]}..."
            ),
            token_transfer=tt,
        )
