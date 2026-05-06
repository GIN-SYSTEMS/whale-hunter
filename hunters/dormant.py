"""
hunters/dormant.py — DormantHunter

Detects transactions from wallets with no activity for >5 years.
Uses a hardcoded set of known genesis-era / ancient dormant wallets
plus a nonce-based heuristic for unknown addresses.
"""

from __future__ import annotations

from typing import Optional

from core.types import Transaction, Signal, SignalCategory, HunterID
from core.config import Config
from utils.addresses import DORMANT_WALLETS
from .base import BaseHunter


class DormantHunter(BaseHunter):
    """Watches for reactivation of ancient dormant wallets."""

    hunter_id = HunterID.DORMANT

    def __init__(self, config: Config):
        super().__init__(config)
        self._known = DORMANT_WALLETS

    def evaluate(self, tx: Transaction) -> Optional[Signal]:
        if getattr(tx, 'value_eth', None) is None or getattr(tx, 'gas_price_gwei', None) is None:
            return None
        from_lower = tx.from_addr.lower()

        # Direct match against known dormant addresses
        if from_lower in self._known:
            score = 70
            if tx.value_eth > 100:
                score += 15
            if tx.value_eth > 1000:
                score += 10
            if tx.nonce <= 5:
                score += 5
            score = min(score, 100)

            return Signal(
                category=SignalCategory.DORMANT_WAKING,
                hunter_id=self.hunter_id,
                score=score,
                transaction=tx,
                label="DORMANT WAKING",
                details=(
                    f"Known ancient wallet reactivated | "
                    f"Value: {tx.value_eth:,.2f} ETH | "
                    f"Nonce: {tx.nonce}"
                ),
            )

        # Heuristic: very low nonce + high value = potentially dormant
        if tx.nonce <= 2 and tx.value_eth > 50:
            score = 45
            if tx.value_eth > 500:
                score += 20
            if tx.nonce == 0:
                score += 10
            score = min(score, 100)

            if score >= 55:
                return Signal(
                    category=SignalCategory.DORMANT_WAKING,
                    hunter_id=self.hunter_id,
                    score=score,
                    transaction=tx,
                    label="DORMANT SUSPECT",
                    details=(
                        f"Low nonce ({tx.nonce}) + high value heuristic | "
                        f"Value: {tx.value_eth:,.2f} ETH"
                    ),
                )

        return None
