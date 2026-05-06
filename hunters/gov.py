"""
hunters/gov.py — GovRadar

Monitors hardcoded public addresses of governments and institutions.
Detects both inflows (seizure deposits) and outflows (liquidation events).
"""

from __future__ import annotations

from typing import Optional

from core.types import Transaction, Signal, SignalCategory, HunterID
from core.config import Config
from utils.addresses import GOVERNMENT_WALLETS
from .base import BaseHunter


class GovRadar(BaseHunter):
    """Watches government/institutional wallet activity."""

    hunter_id = HunterID.GOV

    def __init__(self, config: Config):
        super().__init__(config)
        self._gov = GOVERNMENT_WALLETS

    def evaluate(self, tx: Transaction) -> Optional[Signal]:
        if getattr(tx, 'value_eth', None) is None or getattr(tx, 'gas_price_gwei', None) is None:
            return None
        from_lower = tx.from_addr.lower()
        to_lower = tx.to_addr.lower() if tx.to_addr else ""

        is_outflow = from_lower in self._gov
        is_inflow = to_lower in self._gov

        if not (is_outflow or is_inflow):
            return None

        score = 65
        direction = "OUTFLOW (liquidation)" if is_outflow else "INFLOW (seizure)"

        if is_outflow:
            score += 15  # Outflows are higher signal
        if tx.value_eth > 500:
            score += 10
        if tx.value_eth > 5000:
            score += 10
        score = min(score, 100)

        return Signal(
            category=SignalCategory.INSTITUTIONAL,
            hunter_id=self.hunter_id,
            score=score,
            transaction=tx,
            label="INSTITUTIONAL MOVE",
            details=(
                f"Government wallet {direction} | "
                f"Value: {tx.value_eth:,.2f} ETH | "
                f"Addr: {from_lower[:12]}..."
            ),
        )
