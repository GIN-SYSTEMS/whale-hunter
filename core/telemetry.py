"""
core/telemetry.py — Observability layer.

InstrumentedQueue: asyncio.Queue with metrics.
FrameRateMonitor: Wall-clock throughput tracker.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class QueueMetrics:
    total_enqueued: int = 0
    total_dequeued: int = 0
    total_dropped: int = 0
    peak_depth: int = 0
    _enqueue_times: deque = field(default_factory=lambda: deque(maxlen=10_000))

    @property
    def throughput_per_sec(self) -> float:
        now = time.perf_counter()
        cutoff = now - 1.0
        while self._enqueue_times and self._enqueue_times[0] < cutoff:
            self._enqueue_times.popleft()
        return float(len(self._enqueue_times))

    @property
    def drop_rate_pct(self) -> float:
        total = self.total_enqueued + self.total_dropped
        return (self.total_dropped / total * 100.0) if total else 0.0


class InstrumentedQueue(asyncio.Queue):
    """Drop-in asyncio.Queue with telemetry counters."""

    def __init__(self, maxsize: int = 0):
        super().__init__(maxsize=maxsize)
        self.metrics = QueueMetrics()

    def put_nowait(self, item) -> None:
        try:
            super().put_nowait(item)
            self.metrics.total_enqueued += 1
            self.metrics._enqueue_times.append(time.perf_counter())
            depth = self.qsize()
            if depth > self.metrics.peak_depth:
                self.metrics.peak_depth = depth
        except asyncio.QueueFull:
            self.metrics.total_dropped += 1
            raise

    def get_nowait(self):
        item = super().get_nowait()
        self.metrics.total_dequeued += 1
        return item

    @property
    def fill_pct(self) -> float:
        if self.maxsize == 0:
            return 0.0
        return (self.qsize() / self.maxsize) * 100.0


class FrameRateMonitor:
    """Rolling-window FPS tracker for the processing pipeline.

    Memory contract (Pillar II): _ts is hard-bounded by maxlen as a safety net
    in case clock skew or pruning logic regression lets entries accumulate.
    Under normal operation the time-window prune keeps it well below the cap.
    Sized for 5_000 FPS × 2s window with 2x headroom.
    """

    _TS_MAXLEN = 10_000

    def __init__(self, window_sec: float = 2.0):
        self._window = window_sec
        self._ts: deque[float] = deque(maxlen=self._TS_MAXLEN)
        self._total: int = 0
        self._start: float = time.perf_counter()
        # Watchdog clock — separate from perf_counter to match time.monotonic()
        # used in main.watchdog(). Initialized at construction so the first 5
        # minutes of startup are a grace period (no false stall trigger).
        self.last_tick: float = time.monotonic()

    def tick(self) -> None:
        now = time.perf_counter()
        self._ts.append(now)
        self._total += 1
        cutoff = now - self._window
        while self._ts and self._ts[0] < cutoff:
            self._ts.popleft()
        self.last_tick = time.monotonic()

    @property
    def fps(self) -> float:
        if len(self._ts) < 2:
            return 0.0
        span = self._ts[-1] - self._ts[0]
        return (len(self._ts) - 1) / span if span > 0 else 0.0

    @property
    def avg_fps(self) -> float:
        elapsed = time.perf_counter() - self._start
        return self._total / elapsed if elapsed > 0 else 0.0

    @property
    def total_frames(self) -> int:
        return self._total


class WalletHistoryCache:
    """
    Per-address signal history (last N movements per wallet).

    Memory contract (Pillar II):
      • Hard-bounded by max_wallets — eviction is O(1) via OrderedDict.
      • Eviction is by recency-of-write: a wallet that just had activity
        bumps to the end; the least-recently-active gets evicted.
      • Per-wallet history is a deque(maxlen=per_wallet), so individual
        wallets cannot grow either.
      • Reads do NOT bump recency — manually inspecting an old wallet via
        the TUI archive must not keep it alive at the cost of fresh ones.
    """

    def __init__(self, max_wallets: int = 1_000, per_wallet: int = 5):
        from collections import OrderedDict
        self._cache: "OrderedDict[str, deque]" = OrderedDict()
        self._max_wallets = max_wallets
        self._per_wallet  = per_wallet

    def record(self, signal) -> None:
        addr = signal.transaction.from_addr.lower()
        bucket = self._cache.get(addr)
        if bucket is None:
            if len(self._cache) >= self._max_wallets:
                self._cache.popitem(last=False)  # O(1) LRU evict
            bucket = deque(maxlen=self._per_wallet)
            self._cache[addr] = bucket
        else:
            self._cache.move_to_end(addr)        # O(1) recency bump on write
        bucket.append(signal)

    def get(self, addr: str) -> list:
        bucket = self._cache.get(addr.lower())
        return list(bucket) if bucket is not None else []

    def __len__(self) -> int:
        return len(self._cache)
