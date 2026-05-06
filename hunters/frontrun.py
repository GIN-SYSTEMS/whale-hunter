"""
hunters/frontrun.py — FrontRunDetector

Compares each transaction's gas price against a 60-second rolling window
to detect MEV front-runners and priority gas auctions (PGA) in the mempool.

Scoring:
  > P90 * 1.5x  → score 55   (suspect)
  > P90 * 2.5x  → score 75   (high probability)
  > P90 * 4.0x  → score 90   (near-certain front-run / MEV)
"""

from __future__ import annotations

import time
from collections import deque
from typing import Optional

from core.types import Transaction, Signal, SignalCategory, HunterID
from core.config import Config
from .base import BaseHunter

_WINDOW_SEC       = 60.0   # rolling window duration
_MIN_SAMPLES      = 20     # minimum window depth before firing
_P90_RATIO_LOW    = 1.5    # 50% above P90 → suspect
_P90_RATIO_HIGH   = 2.5    # 150% above P90 → high confidence
_P90_RATIO_CRIT   = 4.0    # 300% above P90 → near-certain
_WINDOW_MAXLEN    = 500    # hard cap on samples (Pillar II safety net;
                           # time-based prune is the primary mechanism)


class FrontRunDetector(BaseHunter):
    """Detects front-running by gas price velocity vs. 60-second rolling window.

    Memory contract (Pillar II): _window is a deque(maxlen=500). Time-based
    pruning is the primary correctness mechanism; maxlen prevents unbounded
    growth under any pathological clock-skew scenario.
    """

    hunter_id = HunterID.FRONTRUN

    def __init__(self, config: Config):
        super().__init__(config)
        # (monotonic_timestamp, gas_price_gwei) pairs
        self._window: deque[tuple[float, float]] = deque(maxlen=_WINDOW_MAXLEN)

    def _p90(self) -> float:
        vals = sorted(g for _, g in self._window)
        idx = max(0, int(len(vals) * 0.9) - 1)
        return vals[idx]

    def evaluate(self, tx: Transaction) -> Optional[Signal]:
        if getattr(tx, 'value_eth', None) is None or getattr(tx, 'gas_price_gwei', None) is None:
            return None
        now = time.monotonic()
        cutoff = now - _WINDOW_SEC

        # Prune expired entries
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()

        fired: Optional[Signal] = None

        if len(self._window) >= _MIN_SAMPLES:
            p90 = self._p90()
            if p90 > 0 and tx.gas_price_gwei > p90 * _P90_RATIO_LOW:
                ratio = tx.gas_price_gwei / p90

                if ratio >= _P90_RATIO_CRIT:
                    score = 90
                elif ratio >= _P90_RATIO_HIGH:
                    score = 75
                else:
                    score = 55

                fired = Signal(
                    category=SignalCategory.FRONT_RUN,
                    hunter_id=self.hunter_id,
                    score=score,
                    transaction=tx,
                    label="FRONT-RUN DETECTED",
                    details=(
                        f"Gas: {tx.gas_price_gwei:,.0f} gwei | "
                        f"P90(60s): {p90:,.0f} gwei | "
                        f"Ratio: {ratio:.1f}x | "
                        f"Samples: {len(self._window)}"
                    ),
                )

        # Record after evaluation (do not pollute window with the spike itself)
        self._window.append((now, tx.gas_price_gwei))
        return fired
