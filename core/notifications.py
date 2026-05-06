# Copyright (C) 2026 Whale Hunter Contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""
core/notifications.py — Sovereign Notification Shield (V11.0).

Discipline doctrine
-------------------
The TUI shows the firehose. The operating system never should. By default
this module stays silent; opt in via .env (OS_NOTIFICATIONS_ENABLED=1) and
even then the shield will only escalate to the OS tray when:

  1. The signal is flagged is_vip                     (operator-curated hit)
  2. OR the transaction value >= os_notify_min_eth    (default 100 ETH)

A wall-clock throttle (default 10 s between fires) guarantees the OS tray
never gets a flood, no matter how busy the mempool gets.

Cross-platform
--------------
  Windows : `winotify` (lazy import — completely optional dependency)
  macOS   : `osascript display notification ...`
  Linux   : `notify-send` (libnotify; standard on every desktop env)
  Anywhere: terminal bell (`\\a`) as a last-resort sonic fallback

If none of the above is available, OS notifications are silently disabled —
the TUI keeps running unaffected. We never crash the sentinel because of
a missing notification backend.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger("whale_hunter.notifications")


# ── Backend probes (lazy, no import cost when disabled) ──────────────────────

def _probe_windows() -> Optional[Callable[[str, str], None]]:
    """Return a (title, body) -> None callable using winotify, or None."""
    if sys.platform != "win32":
        return None
    try:
        from winotify import Notification, audio  # type: ignore
    except ImportError:
        return None

    def _fire(title: str, body: str) -> None:
        try:
            n = Notification(
                app_id="Whale Hunter",
                title=title,
                msg=body,
                duration="short",
            )
            # A muted ping: present but not theatrical.
            try:
                n.set_audio(audio.Default, loop=False)
            except Exception:
                pass
            n.show()
        except Exception as exc:
            log.debug("winotify fire failed: %s", exc)

    return _fire


def _probe_macos() -> Optional[Callable[[str, str], None]]:
    if sys.platform != "darwin":
        return None
    if shutil.which("osascript") is None:
        return None

    def _fire(title: str, body: str) -> None:
        try:
            # Escape double quotes for AppleScript embedding.
            t = title.replace('"', '\\"')
            b = body.replace('"', '\\"')
            script = f'display notification "{b}" with title "{t}"'
            subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            log.debug("osascript fire failed: %s", exc)

    return _fire


def _probe_linux() -> Optional[Callable[[str, str], None]]:
    if sys.platform.startswith("linux") is False:
        return None
    if shutil.which("notify-send") is None:
        return None

    def _fire(title: str, body: str) -> None:
        try:
            subprocess.Popen(
                ["notify-send", "-a", "Whale Hunter", title, body],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            log.debug("notify-send fire failed: %s", exc)

    return _fire


def _probe_terminal_bell() -> Callable[[str, str], None]:
    """Last-resort fallback: emit a single ASCII BEL. Some terminals
    suppress this; that's fine — silent failure is a feature here."""
    def _fire(_title: str, _body: str) -> None:
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass
    return _fire


# ── Public shield ────────────────────────────────────────────────────────────

@dataclass
class _ShieldDecision:
    """Outcome of one shield check — useful in tests + debug logs."""
    fired:        bool
    reason:       str
    cooldown_left: float


class NotificationShield:
    """Throttled, filtered OS desktop notifier.

    The shield holds a single piece of state: the monotonic timestamp of
    the last fire. Every signal goes through `maybe_notify(signal)`; the
    method is deliberately cheap so it can sit on the hot path without
    measurable overhead.
    """

    def __init__(
        self,
        enabled:      bool  = False,
        min_eth:      float = 100.0,
        throttle_sec: float = 10.0,
        backend:      Optional[Callable[[str, str], None]] = None,
    ):
        self._enabled       = bool(enabled)
        self._min_eth       = float(min_eth)
        self._throttle_sec  = float(throttle_sec)
        self._last_fire_mt  = 0.0
        self._suppressed    = 0  # for telemetry
        self._fired         = 0
        self._backend       = backend or self._auto_backend()

    # ── Static factory: pick the best backend for this platform ────────

    @staticmethod
    def _auto_backend() -> Callable[[str, str], None]:
        for probe in (_probe_windows, _probe_macos, _probe_linux):
            backend = probe()
            if backend is not None:
                return backend
        return _probe_terminal_bell()

    # ── Public knobs ───────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def fired(self) -> int:
        return self._fired

    @property
    def suppressed(self) -> int:
        return self._suppressed

    def update_settings(
        self,
        *,
        enabled:      Optional[bool]  = None,
        min_eth:      Optional[float] = None,
        throttle_sec: Optional[float] = None,
    ) -> None:
        """Hot-reload-friendly: tweak filter knobs without reinstantiating."""
        if enabled is not None:
            self._enabled = bool(enabled)
        if min_eth is not None:
            self._min_eth = float(min_eth)
        if throttle_sec is not None:
            self._throttle_sec = float(throttle_sec)

    # ── The hot path ───────────────────────────────────────────────────

    def maybe_notify(self, signal) -> _ShieldDecision:
        """Return a decision record. The TUI keeps showing the signal
        regardless — only the OS tray is gated by this method."""
        if not self._enabled:
            return _ShieldDecision(False, "shield_disabled", 0.0)

        # Filter: must be VIP OR above the value threshold. Everything
        # else is TUI-only.
        is_vip   = bool(getattr(signal, "is_vip", False))
        value    = float(getattr(getattr(signal, "transaction", None), "value_eth", 0.0))
        big_move = value >= self._min_eth

        if not (is_vip or big_move):
            self._suppressed += 1
            return _ShieldDecision(False, "below_threshold", 0.0)

        # Throttle: even VIP/large hits are capped at one fire per window.
        now      = time.monotonic()
        elapsed  = now - self._last_fire_mt
        if elapsed < self._throttle_sec:
            self._suppressed += 1
            return _ShieldDecision(False, "throttled", self._throttle_sec - elapsed)

        # Fire.
        self._last_fire_mt = now
        self._fired += 1

        title, body = self._format(signal, is_vip=is_vip, value_eth=value)
        try:
            self._backend(title, body)
        except Exception as exc:
            log.debug("Backend dispatch failed: %s", exc)

        return _ShieldDecision(True, "fired", self._throttle_sec)

    # ── Formatter ──────────────────────────────────────────────────────

    @staticmethod
    def _format(signal, *, is_vip: bool, value_eth: float) -> tuple[str, str]:
        tx     = getattr(signal, "transaction", None)
        alias  = getattr(signal, "target_alias", None) or "Unknown target"
        chain  = getattr(tx, "network", "ETH") if tx else "ETH"
        h      = (getattr(tx, "tx_hash", "") or "")[:10] if tx else ""

        if is_vip:
            title = f"★ Whale Hunter — VIP move on {chain}"
        else:
            title = f"Whale Hunter — {value_eth:,.2f} {chain}"

        body = f"{alias}  ·  {value_eth:,.4f} {chain}  ·  {h}…"
        return title, body
