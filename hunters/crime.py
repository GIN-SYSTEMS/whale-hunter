"""
hunters/crime.py — CrimeWatch

Monitors outflows from known hack-related addresses and
detects interactions with Tornado Cash mixer contracts.
"""

from __future__ import annotations

from typing import Optional

from core.types import Transaction, Signal, SignalCategory, HunterID
from core.config import Config
from utils.addresses import HACK_ADDRESSES, TORNADO_CONTRACTS
from .base import BaseHunter


class CrimeWatch(BaseHunter):
    """Monitors hack/exploit address activity and mixer interactions."""

    hunter_id = HunterID.CRIME

    def __init__(self, config: Config):
        super().__init__(config)
        self._hacks = HACK_ADDRESSES
        self._tornado = TORNADO_CONTRACTS

    def evaluate(self, tx: Transaction) -> Optional[Signal]:
        if getattr(tx, 'value_eth', None) is None or getattr(tx, 'gas_price_gwei', None) is None:
            return None
        from_lower = tx.from_addr.lower()
        to_lower = tx.to_addr.lower() if tx.to_addr else ""

        # Check for hack address activity
        hack_match = from_lower in self._hacks or to_lower in self._hacks
        # Check for Tornado Cash interaction
        tornado_match = to_lower in self._tornado or from_lower in self._tornado

        if not (hack_match or tornado_match):
            return None

        score = 70
        label_parts = []

        if hack_match:
            score += 10
            label_parts.append("HACK-LINKED")
        if tornado_match:
            score += 15
            label_parts.append("MIXER")
        if tx.value_eth > 100:
            score += 5
        score = min(score, 100)

        label = " + ".join(label_parts) if label_parts else "CRIME FLOW"

        return Signal(
            category=SignalCategory.CRIME_FLOW,
            hunter_id=self.hunter_id,
            score=score,
            transaction=tx,
            label=label,
            details=(
                f"{'Hack' if hack_match else 'Mixer'} address detected | "
                f"Value: {tx.value_eth:,.2f} ETH | "
                f"From: {from_lower[:12]}... -> To: {to_lower[:12]}..."
            ),
        )
