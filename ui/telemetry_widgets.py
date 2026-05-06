"""
ui/telemetry_widgets.py — Live ASCII sparkline telemetry.

Provides a zero-dependency sparkline renderer (▁▂▃▄▅▆▇█) and a
rolling TelemetryBuffer that samples gas price velocity and queue
pressure at a configurable rate.

Designed to be embedded directly in the MetricsBar string — no
extra Textual widget overhead, keeping the render budget minimal.
"""

from __future__ import annotations

from collections import deque

# Block characters ordered low → high intensity
_SPARKS = " ▁▂▃▄▅▆▇█"


def render_sparkline(values: list[float], width: int = 20) -> str:
    """
    Render a sequence of float values as an ASCII sparkline string.

    Returns a string of exactly `width` block characters.
    Empty or flat series → dim baseline (all ▁).
    """
    if not values:
        return "▁" * width

    # Downsample or right-pad to exact width
    n = len(values)
    if n >= width:
        step = n / width
        sampled = [values[int(i * step)] for i in range(width)]
    else:
        sampled = list(values) + [0.0] * (width - n)

    lo = min(sampled)
    hi = max(sampled)
    span = hi - lo

    chars: list[str] = []
    for v in sampled:
        if span == 0:
            idx = 1   # flat baseline → ▁
        else:
            idx = max(1, int((v - lo) / span * (len(_SPARKS) - 1)))
        chars.append(_SPARKS[idx])
    return "".join(chars)


class TelemetryBuffer:
    """
    Rolling sample store for gas velocity and queue pressure.

    Call `record_gas(gwei)` on each transaction and
    `record_queue(fill_pct)` on each metrics tick.
    Both buffers are bounded to `maxlen` samples.
    """

    def __init__(self, maxlen: int = 60) -> None:
        self._gas:   deque[float] = deque(maxlen=maxlen)
        self._queue: deque[float] = deque(maxlen=maxlen)

    # ── Writers (called from hot path / metrics tick) ─────

    def record_gas(self, gwei: float) -> None:
        self._gas.append(gwei)

    def record_queue(self, fill_pct: float) -> None:
        self._queue.append(fill_pct)

    # ── Readers (called from UI render) ──────────────────

    def gas_sparkline(self, width: int = 20) -> str:
        return render_sparkline(list(self._gas), width)

    def queue_sparkline(self, width: int = 12) -> str:
        return render_sparkline(list(self._queue), width)

    @property
    def gas_latest(self) -> float:
        return self._gas[-1] if self._gas else 0.0

    @property
    def queue_latest(self) -> float:
        return self._queue[-1] if self._queue else 0.0
