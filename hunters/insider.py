"""
hunters/insider.py — InsiderScanner

Detects newly created wallets (<7 days old approximated by nonce)
executing transactions above $500k. Low nonce + high value = alpha.
"""

from __future__ import annotations

from typing import Optional

from core.types import Transaction, Signal, SignalCategory, HunterID
from core.config import Config
from .base import BaseHunter

# Nonce threshold: wallets with nonce < this are considered "new"
_MAX_NEW_NONCE = 10


class InsiderScanner(BaseHunter):
    """Detects fresh wallets moving large sums — potential insider activity."""

    hunter_id = HunterID.INSIDER

    def __init__(self, config: Config):
        super().__init__(config)
        self._threshold_usd = config.insider_threshold_usd

    def evaluate(self, tx: Transaction) -> Optional[Signal]:
        if getattr(tx, 'value_eth', None) is None or getattr(tx, 'gas_price_gwei', None) is None:
            return None
        # Only care about low-nonce senders
        if tx.nonce > _MAX_NEW_NONCE:
            return None

        value_usd = tx.value_usd(self.config.eth_price_usd)
        if value_usd < self._threshold_usd:
            return None

        # Score calculation
        score = 55
        if tx.nonce <= 2:
            score += 15
        if tx.nonce == 0:
            score += 10
        if value_usd > 1_000_000:
            score += 10
        if value_usd > 5_000_000:
            score += 10
        score = min(score, 100)

        return Signal(
            category=SignalCategory.INSIDER_ALPHA,
            hunter_id=self.hunter_id,
            score=score,
            transaction=tx,
            label="INSIDER ALPHA",
            details=(
                f"New wallet (nonce={tx.nonce}) moving "
                f"${value_usd:,.0f} | "
                f"Value: {tx.value_eth:,.2f} ETH"
            ),
        )
