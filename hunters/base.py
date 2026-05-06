"""
hunters/base.py — Abstract base class for all hunter engines.

Every hunter receives a Transaction, returns Optional[Signal].
Hunters are pure CPU — no I/O allowed in the hot path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from core.types import Transaction, Signal, HunterID
from core.config import Config


class BaseHunter(ABC):
    """Interface contract for all heuristic hunter engines."""

    hunter_id: HunterID

    def __init__(self, config: Config):
        self.config = config

    @abstractmethod
    def evaluate(self, tx: Transaction) -> Optional[Signal]:
        if getattr(tx, 'value_eth', None) is None or getattr(tx, 'gas_price_gwei', None) is None:
            return None
        """Evaluate a transaction. Return Signal if match, None otherwise."""
        ...

    def __repr__(self) -> str:
        return f"<{self.hunter_id.value}>"
