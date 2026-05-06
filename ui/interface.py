"""
ui/interface.py — Brutalist TUI for Whale Hunter v11.0 — SOVEREIGN.

Layout:
  Left  (40 cols) : Tactical panel — ASCII signal art + selectable history
  Right (1fr)     : Intelligence stream — dense real-time data feed
  Bottom bar      : Live metrics + Gas/Queue sparklines + mode indicators

v3.0 additions:
  [Space/P]  Global Pause / Resume  (buffer keeps accumulating while frozen)
  [I]        Open Signal Archive (DataTable)  →  [Enter] Full inspection
  [C]        Re-open Initialization screen to change credentials
  [d]        Demo Mode (scripted 3-event walkthrough)
  [F1]       Help / Man Page
  Reconnect banner: shown when WSS drops, auto-hides on recovery

Palette: Orange #ff8c00 / Amber #d4a017 / Terminal-Green / Red.
No emojis. Industrial / brutalist aesthetic.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, Grid
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Footer, Header, RichLog, Static, Input, Button, DataTable, ContentSwitcher,
)
from rich.text import Text
from rich.panel import Panel
from rich.table import Table
from rich.box  import HEAVY, ROUNDED

from core.telemetry import FrameRateMonitor, WalletHistoryCache
from core.types import (
    SIGNAL_ART, Signal, SignalCategory,
    wallet_age_label, wallet_tier,
)
from ui.telemetry_widgets import TelemetryBuffer


# ── Batch render constants ─────────────────────────────────────────────────────

_BURST_THRESHOLD = 500
_DRAIN_NORMAL    = 50
_DRAIN_BURST     = 120
_BURST_MIN_SCORE = 70
_MAX_HISTORY     = 100   # signals kept in memory (deque maxlen — see _signals)
# Periodic intel-log clear: safety valve against any RichLog buffer creep
# beyond Textual's max_lines internal cap. Every N batch flushes we wipe.
_INTEL_LOG_CLEAR_EVERY = 200


# ── App-wide CSS ──────────────────────────────────────────────────────────────

APP_CSS = """
Screen {
    layout: vertical;
    background: #0d0d0d;
    color: #b0b0b0;
    layers: base overlay;
}

/* ── Reconnect banner (docked top, hidden by default) ── */
#reconnect-banner {
    dock: top;
    height: 1;
    content-align: center middle;
    background: #080018;
    color: #7050b0;
    display: none;
}
#reconnect-banner.visible {
    display: block;
}

/* ── Pause banner inside tactical pane ── */
#pause-banner {
    height: 1;
    content-align: center middle;
    background: #001020;
    color: #4080b0;
    text-style: bold;
    display: none;
}
#pause-banner.visible {
    display: block;
}

#main-area {
    height: 1fr;
    padding: 1 2; /* Breathing space */
}

/* ── Tactical left pane ── */
#tactical {
    width: 40;
    border: solid #333333;
    margin-right: 2; /* Breathing space */
    padding: 0 1;
    overflow-y: auto;
    background: #111111;
}
#tactical:focus-within {
    border: solid #555555;
}
#tactical.paused {
    border: solid #204060;
    background: #0a111a;
}

#tactical-signal {
    height: auto;
    margin: 1 0;
    color: #cccccc;
}

#tactical-history {
    height: 1fr;
    color: #777777;
}

/* ── Intelligence right pane ── */
#intelligence {
    width: 1fr;
    border: solid #333333;
    background: #0a0a0a;
    padding: 0 1;
}
#intelligence:focus-within {
    border: solid #555555;
}

#intel-log {
    height: 1fr;
    background: #0a0a0a;
    color: #cccccc;
    border: blank;
}

#intel-log > .datatable--header {
    background: #111111;
    color: #888888;
    text-style: bold;
}

#intel-log > .datatable--cursor {
    background: #2a2a2a;
    color: #ffffff;
    text-style: bold;
}

#intel-log > .datatable--hover {
    background: #1a1a1a;
}

/* ── Metrics bar ── */
#metrics-bar {
    dock: bottom;
    height: 3;
    padding: 0 2;
    background: #111111;
    color: #888888;
    border-top: solid #222222;
}

/* ── ContentSwitcher (radar / archive / analytics) ── */
#view-switcher {
    height: 1fr;
    background: #0d0d0d;
}

/* ── Archive view (full-width history) ── */
#archive-view {
    height: 1fr;
    padding: 1 2;
    background: #0d0d0d;
}
#archive-title {
    height: 2;
    content-align: center middle;
    background: #111111;
    color: #c0a060;
    text-style: bold;
    border-bottom: solid #222222;
}
#archive-table {
    height: 1fr;
    background: #0d0d0d;
    color: #cccccc;
    border: solid #333333;
}
#archive-table > .datatable--header {
    background: #111111;
    color: #c0a060;
    text-style: bold;
}
#archive-table > .datatable--cursor {
    background: #2a2a2a;
    color: #ffffff;
    text-style: bold;
}

/* ── Analytics view (system health) ── */
#analytics-view {
    height: 1fr;
    padding: 1 2;
    background: #0d0d0d;
}
#analytics-title {
    height: 2;
    content-align: center middle;
    background: #111111;
    color: #c0a060;
    text-style: bold;
    border-bottom: solid #222222;
}
#analytics-body {
    height: 1fr;
    color: #cccccc;
    padding: 1 2;
    background: #111111;
    border: solid #333333;
    margin-top: 1;
}

HelpScreen, DemoScreen {
    align: center middle;
}
"""


# ── Modals: Command Palette & Help ────────────────────────────────────────────

_COMMAND_CSS = """
CommandPaletteScreen {
    align: center middle;
    background: rgba(0, 0, 0, 0.6);
}
#cmd-box {
    width: 60;
    height: 5;
    border: solid #444444;
    background: #111111;
    padding: 1 2;
}
#cmd-input {
    border: blank;
    background: #111111;
    color: #ffffff;
}
"""

class CommandPaletteScreen(ModalScreen[str]):
    """Floating Command Palette for sleek unobtrusive input."""
    CSS = _COMMAND_CSS
    BINDINGS = [Binding("escape", "dismiss_empty", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="cmd-box"):
            yield Input(placeholder="Quick command (:logs, :clear, :quit, /min 50)", id="cmd-input")

    def on_mount(self) -> None:
        inp = self.query_one(Input)
        inp.value = ":"
        inp.focus()

    def action_dismiss_empty(self) -> None:
        self.dismiss("")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)


_HELP_CSS = """
HelpScreen {
    align: center middle;
    background: rgba(0, 0, 0, 0.85);
}
#help-outer {
    width: 80;
    height: 24;
    border: solid #333333;
    background: #0d0d0d;
    padding: 1 2;
}
#help-title {
    height: 2;
    content-align: center middle;
    background: #111111;
    color: #888888;
    border-bottom: solid #222222;
    text-style: bold;
    margin-bottom: 1;
}
#help-table {
    height: 1fr;
    background: #0d0d0d;
    border: blank;
}
}
#help-footer {
    height: 1;
    content-align: center middle;
    color: #555555;
    border-top: dashed #222222;
    margin-top: 1;
}

/* ── Target Command Center ── */
#target-modal {
    width: 80%;
    height: 80%;
    background: #0d0d0d;
    border: heavy #ff00ff;
    padding: 1 2;
}
#target-title {
    content-align: center middle;
    text-style: bold;
    color: #ff00ff;
    width: 100%;
}
#target-subtitle {
    content-align: center middle;
    color: #b0b0b0;
    width: 100%;
    margin-bottom: 1;
}
#target-table {
    height: 1fr;
    margin-bottom: 1;
    border: solid #333333;
}
#target-inputs {
    height: 3;
    margin-bottom: 1;
}
#input-address {
    width: 3fr;
    margin-right: 1;
    border: solid #2a323e;
}
#input-label {
    width: 2fr;
    border: solid #2a323e;
}
#target-buttons {
    height: 3;
    align: center middle;
}
"""

class HelpScreen(ModalScreen):
    """Clean, structured Help Screen with DataTable for bindings."""
    CSS = _HELP_CSS
    BINDINGS = [("escape", "dismiss", "Close"), ("asterisk", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-outer"):
            yield Static("  WHALE HUNTER v11.0 — SOVEREIGN  ·  [*] HELP & KEYBINDINGS", id="help-title")
            yield DataTable(id="help-table", cursor_type="none", show_cursor=False, zebra_stripes=True)
            yield Static("  [ESC] or [*] — close panel", id="help-footer")

    def on_mount(self) -> None:
        table = self.query_one("#help-table", DataTable)
        table.add_columns("Hotkey", "Action", "Description")
        
        table.add_rows([
            (Text("q", style="bold cyan"), "Quit", "Exit the application gracefully"),
            (Text("Space / P", style="bold cyan"), "Pause / Resume", "Freeze display (buffer still fills)"),
            (Text(":", style="bold cyan"), "Command Palette", "Open floating quick command bar"),
            (Text("I", style="bold cyan"), "Signal Archive", "Inspect & export buffered signals"),
            (Text("F", style="bold cyan"), "Volume Filter", "Set/clear minimum ETH threshold"),
            (Text("C", style="bold cyan"), "Configuration", "Open Vault config modal"),
            (Text("d", style="bold cyan"), "Demo Mode", "Run script demo walkthrough"),
            (Text("*", style="bold cyan"), "Help", "Show this help screen"),
        ])

    def action_dismiss(self) -> None:
        self.dismiss()

_FILTER_CSS = """
FilterScreen {
    align: center middle;
    background: rgba(10, 14, 20, 0.85);
}
#flt-box {
    width: 64;
    height: auto;
    border: solid #2a323e;
    background: #0d141c;
    padding: 1 2;
}
#flt-title {
    height: 2;
    content-align: center middle;
    color: #c0a060;
    text-style: bold;
    border-bottom: solid #2a323e;
    margin-bottom: 1;
}
#flt-prompt {
    color: #8a929e;
    margin-top: 1;
}
#flt-current {
    color: #6b8aa8;
    margin-bottom: 1;
}
#flt-input {
    border: solid #2a323e;
    background: #11161e;
    color: #c0a060;
    height: 3;
}
#flt-input:focus {
    border: solid #c0a060;
}
#flt-error {
    color: #e85060;
    height: 1;
    margin-top: 1;
}
#flt-footer {
    height: 1;
    content-align: center middle;
    color: #555c66;
    border-top: dashed #2a323e;
    margin-top: 1;
}
"""


class FilterScreen(ModalScreen):
    """Dedicated single-field minimum-ETH filter modal.

    Replaces the unreliable Ctrl+P CommandProvider path. Pressing [f] toggles
    this; Enter on empty resets, Enter on a number sets the minimum and
    triggers an instant re-render of the deque-backed signal buffer. The
    multiprocessing pipeline is untouched — this is pure display-side state.
    """

    CSS = _FILTER_CSS
    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("f",      "dismiss", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="flt-box"):
            yield Static("  VOLUME FILTER  —  MINIMUM ETH", id="flt-title")
            yield Static("  Enter Minimum ETH (or leave empty to reset):", id="flt-prompt")
            yield Static("", id="flt-current")
            yield Input(id="flt-input", placeholder="e.g. 50  |  empty = show all")
            yield Static("", id="flt-error")
            yield Static("  [Enter] apply  ·  [ESC]/[f] cancel", id="flt-footer")

    def on_mount(self) -> None:
        # Show the current state and pre-fill if a filter is already active.
        current = getattr(self.app, "filter_min_eth", None)
        cur_widget = self.query_one("#flt-current", Static)
        inp        = self.query_one("#flt-input", Input)
        if current is None:
            cur_widget.update("  Currently: [dim]no filter — full firehose[/dim]")
        else:
            cur_widget.update(f"  Currently: [#c0a060]>= {current} ETH[/#c0a060]")
            inp.value = str(current)
        inp.focus()

    def action_dismiss(self) -> None:
        self.dismiss()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "flt-input":
            return
        raw = (event.value or "").strip()
        err_widget = self.query_one("#flt-error", Static)

        if not raw:
            # Empty → reset
            self.app.filter_min_eth = None
            self.app.filter_max_eth = None
            self.app.notify("Volume filter cleared — full firehose restored.", title="Filter")
        else:
            try:
                value = float(raw)
            except ValueError:
                err_widget.update(f"  Invalid number: '{raw[:32]}'")
                return
            if value < 0:
                err_widget.update("  ETH threshold must be >= 0")
                return
            self.app.filter_min_eth = value
            self.app.filter_max_eth = None
            self.app.notify(
                f"Volume filter active: >= {value} ETH",
                title="Filter",
            )

        # Re-render the active deque buffer + intel-log + history side panel.
        try:
            self.app._apply_filters()
        except Exception as exc:
            err_widget.update(f"  Apply failed: {exc}")
            return

        self.dismiss()


_CONFIG_CSS = """
ConfigScreen {
    align: center middle;
    background: rgba(10, 14, 20, 0.85);
}
#cfg-box {
    width: 80;
    height: auto;
    border: solid #2a323e;
    background: #0d141c;
    padding: 1 2;
}
#cfg-title {
    height: 2;
    content-align: center middle;
    color: #c0a060;
    text-style: bold;
    border-bottom: solid #2a323e;
    margin-bottom: 1;
}
.cfg-label {
    color: #8a929e;
    margin-top: 1;
}
#cfg-save {
    margin-top: 1;
    width: 100%;
}
#cfg-footer {
    height: 1;
    content-align: center middle;
    color: #555c66;
    border-top: dashed #2a323e;
    margin-top: 1;
}
"""

class ConfigScreen(ModalScreen):
    CSS = _CONFIG_CSS
    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("c",      "dismiss", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="cfg-box"):
            yield Static("  API & TELEGRAM CONFIGURATION", id="cfg-title")
            yield Static("Alchemy / Provider WSS URL:", classes="cfg-label")
            yield Input(
                id="cfg-wss",
                placeholder="wss:// or https:// — auto-converted to wss://",
            )
            yield Static("Telegram Bot Token:", classes="cfg-label")
            yield Input(
                id="cfg-telegram",
                password=True,
                placeholder="leave blank to disable Telegram",
            )
            yield Static("Telegram Chat ID:", classes="cfg-label")
            yield Input(
                id="cfg-chat-id",
                placeholder="numeric (e.g. 123456789 or -100123456789 for groups)",
            )
            yield Button("  Save & Encrypt to Vault", id="cfg-save", variant="warning")
            yield Static("  [ESC] / [c] — close configuration", id="cfg-footer")

    def on_mount(self) -> None:
        import os
        from core.security import Vault
        wss_url   = Vault.decrypt(os.environ.get("WSS_URL", ""))
        bot_token = Vault.decrypt(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
        chat_id   = os.environ.get("TELEGRAM_CHAT_ID", "")
        wss_input = self.query_one("#cfg-wss", Input)
        wss_input.value = wss_url
        self.query_one("#cfg-telegram", Input).value = bot_token
        self.query_one("#cfg-chat-id", Input).value = chat_id
        wss_input.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cfg-save":
            self._save_config()

    def action_dismiss(self) -> None:
        self.dismiss()

    def _save_config(self) -> None:
        from core.security import Vault
        import os

        raw_wss      = self.query_one("#cfg-wss", Input).value.strip()
        new_telegram = self.query_one("#cfg-telegram", Input).value.strip()
        new_chat_id  = self.query_one("#cfg-chat-id", Input).value.strip()

        # ── Smart URL Parsing: auto-convert https:// → wss:// ────────────────
        for prefix in ("https://", "http://"):
            if raw_wss.startswith(prefix):
                raw_wss = "wss://" + raw_wss[len(prefix):]
                self.app.notify(
                    f"URL auto-converted to wss://",
                    severity="information",
                    timeout=4.0,
                )
                break
        new_wss = raw_wss

        # ── Chat ID validation ────────────────────────────────────────────────
        if new_chat_id:
            clean_id = new_chat_id.lstrip("-")
            if not clean_id.isdigit():
                self.app.notify(
                    "Chat ID must be numeric (e.g. 123456789 or -100123456789).",
                    severity="error",
                )
                return

        current_wss = Vault.decrypt(os.environ.get("WSS_URL", ""))
        wss_changed = new_wss != current_wss

        enc_wss      = Vault.encrypt(new_wss) if new_wss else ""
        enc_telegram = Vault.encrypt(new_telegram) if new_telegram else ""

        env_path = getattr(self.app, "_env_path", None)
        if env_path is None:
            self.app.notify("No .env path resolved — cannot save.", severity="error")
            return

        # Read existing .env, patch the relevant keys, write back.
        existing_lines: list[str] = []
        if env_path.exists():
            existing_lines = env_path.read_text(encoding="utf-8").splitlines()

        new_lines: list[str] = []
        wss_found = tg_found = tg_enabled_found = chat_id_found = False
        for line in existing_lines:
            if line.startswith("WSS_URL="):
                new_lines.append(f"WSS_URL={enc_wss}")
                wss_found = True
            elif line.startswith("TELEGRAM_BOT_TOKEN="):
                new_lines.append(f"TELEGRAM_BOT_TOKEN={enc_telegram}")
                tg_found = True
            elif line.startswith("TELEGRAM_ENABLED="):
                new_lines.append(f"TELEGRAM_ENABLED={'1' if new_telegram else '0'}")
                tg_enabled_found = True
            elif line.startswith("TELEGRAM_CHAT_ID="):
                new_lines.append(f"TELEGRAM_CHAT_ID={new_chat_id}")
                chat_id_found = True
            else:
                new_lines.append(line)

        if not wss_found:
            new_lines.append(f"WSS_URL={enc_wss}")
        if not tg_found:
            new_lines.append(f"TELEGRAM_BOT_TOKEN={enc_telegram}")
        if not tg_enabled_found:
            new_lines.append(f"TELEGRAM_ENABLED={'1' if new_telegram else '0'}")
        if not chat_id_found:
            new_lines.append(f"TELEGRAM_CHAT_ID={new_chat_id}")

        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

        # Refresh os.environ so re-opening ConfigScreen reads updated values.
        os.environ["WSS_URL"]            = enc_wss
        os.environ["TELEGRAM_BOT_TOKEN"] = enc_telegram
        os.environ["TELEGRAM_ENABLED"]   = "1" if new_telegram else "0"
        os.environ["TELEGRAM_CHAT_ID"]   = new_chat_id

        self.app.notify(
            "Credentials encrypted and saved to Vault.",
            title="Vault",
            severity="information",
        )
        if wss_changed:
            self.app.notify(
                "Restart required to apply the new WSS connection.",
                severity="warning",
                timeout=8.0,
            )

        self.dismiss()


# ── Reconnect overlay ─────────────────────────────────────────────────────────

_RECONNECT_CSS = """
ReconnectOverlay {
    align: center middle;
}
#rc-box {
    width: 62;
    height: 11;
    border: solid #4080b0;
    background: #050d18;
    padding: 1 2;
}
#rc-title {
    height: 2;
    content-align: center middle;
    color: #4080b0;
    text-style: bold;
    border-bottom: solid #1a3050;
}
#rc-msg {
    height: 1fr;
    content-align: center middle;
    color: #d4a017;
}
#rc-footer {
    height: 1;
    content-align: center middle;
    color: #4a4030;
    border-top: dashed #1a3050;
}
"""


class ReconnectOverlay(ModalScreen):
    """Non-blocking overlay shown when WSS connection drops."""

    CSS      = _RECONNECT_CSS
    BINDINGS = [Binding("escape", "dismiss", "Dismiss")]

    def __init__(self, initial_msg: str = "Connection lost. Retrying...", **kwargs):
        super().__init__(**kwargs)
        self._initial_msg = initial_msg

    def compose(self) -> ComposeResult:
        with Vertical(id="rc-box"):
            yield Static("  WSS CONNECTION LOST", id="rc-title")
            yield Static(self._initial_msg, id="rc-msg")
            yield Static(
                "  Pipeline retrying in background  —  [ESC] dismiss overlay",
                id="rc-footer",
            )

    def update_message(self, msg: str) -> None:
        try:
            self.query_one("#rc-msg", Static).update(msg)
        except Exception:
            pass

    def action_dismiss(self) -> None:
        self.dismiss()


# ── Tactical pane sub-widgets ─────────────────────────────────────────────────

class TacticalSignal(Static):
    """v6.1 — Sovereign tactical HUD. Hosts a rich.Panel set via
    Static.update(). No render() override; we let Static delegate to its
    internal renderable so update(panel) actually paints a Panel."""


class TacticalHistory(Static):
    history_text: reactive[str] = reactive("  No signals yet.", layout=True)

    def render(self) -> str:
        return self.history_text


class MetricsBar(Static):
    metrics_text: reactive[str] = reactive("  Initializing...", layout=True)

    def render(self) -> str:
        return self.metrics_text


# ── Main application ──────────────────────────────────────────────────────────

class WhaleHunterApp(App):
    """Brutalist TUI for Whale Hunter: Sovereign-Grade On-Chain Intelligence Radar."""

    TITLE     = "WHALE HUNTER v11.0 — SOVEREIGN"
    SUB_TITLE = "Sovereign-Grade Intelligence"
    CSS       = APP_CSS

    BINDINGS = [
        Binding("q",      "quit",          "Quit"),
        Binding("space",  "toggle_pause",  "Pause/Resume", show=True),
        Binding("p",      "toggle_pause",  "Pause/Resume", show=False),
        Binding("t",      "target_center", "Targets"),
        Binding("c",      "config",        "Settings"),
        Binding("asterisk", "help",        "Help", show=False),
        Binding("colon",  "focus_quick_bar", "Quick Cmd", show=False),
        Binding("slash",  "focus_quick_bar", "Quick Cmd", show=False),
        Binding("escape", "cancel_quick_bar", "Cancel",  show=False),
    ]

    _paused:       reactive[bool] = reactive(False)
    _reconnecting: reactive[bool] = reactive(False)
    _heartbeat:    reactive[bool] = reactive(False)
    display_threshold: int = 0
    filter_min_eth: Optional[float] = None
    filter_max_eth: Optional[float] = None

    def __init__(
        self,
        signal_queue: asyncio.Queue,
        monitor: FrameRateMonitor,
        wallet_cache: WalletHistoryCache,
        exports_dir: Path,
        metrics_getter: Optional[Callable] = None,
        env_path: Optional[Path] = None,
        chain_type: str = "ETH",
        **kwargs,
    ):
        # Pop our custom kwargs BEFORE delegating to Textual's App.__init__,
        # otherwise super() raises TypeError on the unknown keyword.
        self._app_ready      = kwargs.pop("app_ready", None)
        self._hot_reload     = kwargs.pop("hot_reload", None)
        self._chain_type     = (chain_type or "ETH").upper()
        super().__init__(**kwargs)
        self._signal_queue   = signal_queue
        self._monitor        = monitor
        self._wallet_cache   = wallet_cache
        self._exports_dir    = exports_dir
        self._metrics_getter = metrics_getter
        self._env_path       = env_path
        # Bounded ring buffer (Pillar II): auto-evicts oldest when full,
        # eliminates the manual list[-N:] copy that fragmented the heap.
        self._signals: deque[Signal] = deque(maxlen=_MAX_HISTORY)
        self._total_signals  = 0
        self._total_alerts   = 0
        self._demo_active    = False
        self._telemetry      = TelemetryBuffer(maxlen=60)
        self._reconnect_overlay: Optional[ReconnectOverlay] = None
        # Counter for the intel-log periodic clear safety valve.
        self._intel_flushes  = 0
        # DataTable row tracking — mirrors _signals deque.
        # _intel_row_keys preserves insertion order; _intel_signals_by_key
        # maps row key → Signal for click-to-inspect dispatch.
        self._intel_row_keys: deque[str] = deque(maxlen=_MAX_HISTORY)
        self._intel_signals_by_key: dict[str, Signal] = {}
        # Sovereign Terminal v4.0: cumulative ETH processed counter, used
        # by the metrics bar and the analytics view. Updated per-batch in
        # _consume_signals from the same Signals the deque records.
        self._total_eth: float = 0.0
        # V8.5: WSS activity pulse — tracks last raw message time for the
        # "I Am Alive" indicator in the MetricsBar.
        self._last_pulse_ts: float = 0.0
        self._raw_count: int = 0
        # Cached current view id so we know whether to do live updates of
        # the archive / analytics panels.
        self._current_view: str = "radar-view"

    # ── Layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="reconnect-banner")
        with ContentSwitcher(initial="radar-view", id="view-switcher"):
            # ── [1] RADAR — live tactical + intel-log dashboard ─────────────
            with Horizontal(id="radar-view"):
                with Vertical(id="tactical"):
                    yield Static("", id="pause-banner")
                    yield TacticalSignal(id="tactical-signal")
                    yield TacticalHistory(id="tactical-history")
                with Vertical(id="intelligence"):
                    yield DataTable(
                        id="intel-log",
                        zebra_stripes=True,
                        cursor_type="row",
                        show_cursor=True,
                    )
            # ── [2] ARCHIVE — full-width history of buffered signals ────────
            with Vertical(id="archive-view"):
                yield Static(
                    "  SIGNAL ARCHIVE  —  [Enter] inspect  ·  [1] back to Radar",
                    id="archive-title",
                )
                yield DataTable(
                    id="archive-table",
                    zebra_stripes=True,
                    cursor_type="row",
                    show_cursor=True,
                )
            # ── [3] ANALYTICS — daemon + pipeline + system health ───────────
            with Vertical(id="analytics-view"):
                yield Static(
                    "  SYSTEM ANALYTICS  —  Daemon · Pipeline · Telemetry",
                    id="analytics-title",
                )
                yield Static("", id="analytics-body")
        yield MetricsBar(id="metrics-bar")
        yield Footer()

    async def on_mount(self) -> None:
        # Configure the intel-log DataTable columns once at mount.
        # v6.0 — added "Chain" column at the end so multi-chain ingestion
        # is visible at a glance with per-chain colour coding.
        intel: DataTable = self.query_one("#intel-log", DataTable)
        # V8.2 Minimalist column schema: Target alias first, Score/Tier removed.
        intel.add_columns("Time", "Chain", "Target", "Activity", "Value", "Gas", "Direction")

        archive: DataTable = self.query_one("#archive-table", DataTable)
        archive.add_columns("Time", "Chain", "Target", "Activity", "Value", "Gas", "Direction")

        # v6.1 — initial tactical HUD placeholder so the left pane isn't
        # blank before the first signal arrives.
        tactical = self.query_one("#tactical-signal", TacticalSignal)
        self._refresh_watchlist_status(tactical)

        self._consume_timer = self.set_interval(0.05, self._consume_signals)
        self.set_interval(1.0,  self._update_metrics)
        # Heartbeat blink confirms the metrics tick is alive
        # independently of FPS — flips ●/○ once per second.
        self.set_interval(1.0,  self._toggle_heartbeat)
        # Analytics view refreshes lazily when active.
        self.set_interval(1.0,  self._update_analytics_if_active)

        # Initial sub-title — overwritten by _apply_filters when a filter
        # is set. Includes active chain so the operator can confirm at a
        # glance which network they're listening to.
        self.sub_title = self._format_subtitle()

        # Focus intel-log so the very first 1/2/3 / m / f keystroke
        # propagates to the App bindings instead of vanishing into the
        # default screen-level focus target.
        try:
            intel.focus()
        except Exception:
            pass

    def _format_subtitle(self, *, filter_overlay: str = "") -> str:
        """Return the canonical Header sub_title string.

        Always shows the active network so multi-chain operators can
        confirm at a glance. Filter / multi-chain overlays append on the
        right of the network tag.
        """
        chain = getattr(self, "_chain_type", "ETH")
        net = "MULTI-CHAIN" if chain.upper() in ("MULTI", "MULTICHAIN") else chain.upper()
        base = f"[ NETWORK: {net} ]"
        if filter_overlay:
            return f"{base}  ·  {filter_overlay}"
        return f"{base}  ·  Sovereign-Grade Intelligence"

    def set_consume_interval(self, seconds: float) -> None:
        self._consume_timer.stop()
        self._consume_timer = self.set_interval(seconds, self._consume_signals)

    def on_ready(self) -> None:
        if self._app_ready:
            self._app_ready.set()

    # ── Reactive watchers ─────────────────────────────────────────────────────

    def watch__paused(self, paused: bool) -> None:
        tactical = self.query_one("#tactical")
        banner   = self.query_one("#pause-banner", Static)
        if paused:
            tactical.add_class("paused")
            banner.update("  ═══  FROZEN — PIPELINE BUFFERING  ═══  [Space/P] to resume")
            banner.add_class("visible")
        else:
            tactical.remove_class("paused")
            banner.update("")
            banner.remove_class("visible")

    def watch__reconnecting(self, reconnecting: bool) -> None:
        banner = self.query_one("#reconnect-banner", Static)
        if reconnecting:
            banner.update("  WSS RECONNECTING — pipeline retrying with exponential backoff...")
            banner.add_class("visible")
        else:
            banner.update("")
            banner.remove_class("visible")
            if self._reconnect_overlay is not None:
                try:
                    self._reconnect_overlay.dismiss()
                except Exception:
                    pass
                self._reconnect_overlay = None

    # ── Signal consumption ────────────────────────────────────────────────────

    def _consume_signals(self) -> None:
        """
        Drain the signal queue and update the display.

        Burst mode (fps > 500 tx/s):
          - Drain up to 120 per tick
          - Update tactical only for the highest-scoring signal
          - Batch intel log writes into a single call
        Normal mode:
          - Drain up to 50 per tick, individual renders
        Paused:
          - Drain from queue (to prevent it filling up) but skip all UI updates.
            The internal self._signals list still accumulates for inspection.
        """
        if self._demo_active:
            return

        fps   = self._monitor.fps
        burst = fps > _BURST_THRESHOLD
        limit = _DRAIN_BURST if burst else _DRAIN_NORMAL

        batch: list[Signal] = []
        while len(batch) < limit:
            try:
                batch.append(self._signal_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not batch:
            return

        self._total_signals += len(batch)

        # Always record in history buffer (even when paused) for inspection.
        # deque(maxlen=_MAX_HISTORY) auto-evicts from the left — no slicing,
        # no heap churn.
        self._signals.extend(batch)

        # Always record wallet history + cumulative ETH.
        for sig in batch:
            self._wallet_cache.record(sig)
            self._telemetry.record_gas(sig.transaction.gas_price_gwei)
            self._total_eth += sig.transaction.value_eth

        # Skip rendering when paused — queue drains but display freezes
        if self._paused:
            return

        intel: DataTable = self.query_one("#intel-log", DataTable)
        render_batch = []
        for s in batch:
            if s.score < self.display_threshold:
                continue
            if self.filter_min_eth is not None and s.transaction.value_eth < self.filter_min_eth:
                continue
            if self.filter_max_eth is not None and s.transaction.value_eth > self.filter_max_eth:
                continue
            render_batch.append(s)

        # v6.1.1 — buffer/lock scrapped. Rows always flow to the live
        # DataTable. The "jump" is solved at the row-eviction level via
        # _append_intel_row() viewport anchoring (cursor + scroll_y
        # compensation). See its docstring for the math.
        if burst:
            if render_batch:
                best = max(render_batch, key=lambda s: s.score)
                if best.score >= _BURST_MIN_SCORE:
                    self._update_tactical(best)
                for sig in render_batch:
                    self._append_intel_row(intel, sig)
        else:
            for sig in render_batch:
                self._update_tactical(sig)
                self._append_intel_row(intel, sig)

        # V11.0 — bell silenced. The terminal bell (\a) was the actual
        # "Windows notification spam" source: every VIP-hit batch fired
        # self.bell(), which Windows Terminal converts into a system
        # sound or toast. We now route VIP escalation exclusively
        # through the NotificationShield in main.py — throttled to one
        # per 10 s, gated by VIP / 100-ETH filter. The TUI row already
        # carries all context, so the bell was decorative anyway.
        # Intentionally a no-op: keep this branch as documentation.
        # vip_in_batch = [s for s in render_batch if getattr(s, "is_vip", False)]
        # (no bell)

        # Periodic safety-valve clear of the intel-log. The deque-mirror
        # eviction path keeps the table bounded normally; this is defence
        # in depth against any renderer ref leak.
        self._intel_flushes += 1
        if self._intel_flushes >= _INTEL_LOG_CLEAR_EVERY:
            self._intel_flushes = 0
            self._intel_clear_table()

        self._update_history()

    def _append_intel_row(self, table: "DataTable", sig: Signal) -> None:
        """Add a clickable row mirroring the _signals deque maxlen.

        v6.1.1 — Seamless Viewport Anchoring.
            Newest rows are appended at the bottom of the DataTable; the
            oldest is evicted from the top when the deque is full. This
            shifts every row's index up by 1, which historically yanked
            the operator's cursor (and viewport) UP by one row each
            eviction — the visible "jump" they reported.

            We compensate at the row-eviction boundary:
              1. Snapshot (cursor.row, cursor.column, scroll_y) BEFORE
                 the remove if the table is focused AND the cursor is
                 below the top.
              2. Perform the remove + add as normal.
              3. Re-anchor: cursor.row -= 1, scroll_y -= 1 (clamped to 0)
                 so the operator's logical row stays in the same screen
                 cell. The viewport is invisibly stationary while data
                 keeps streaming.

            When the cursor is at row 0 OR the table doesn't have focus,
            we let the default behaviour run (no anchor needed; either
            the operator is watching live or isn't engaged).

            Safe under burst — every per-row call self-anchors, so a
            batch of N evictions decrements the cursor by N total.
        """
        will_evict = len(self._intel_row_keys) >= self._intel_row_keys.maxlen
        current_row_count = len(self._intel_row_keys)

        # ── Snapshot anchor & Live Edge Detection ───────────────────────
        anchor: Optional[tuple[int, int, int]] = None
        is_live_edge = False
        col_index = 0

        try:
            cc = table.cursor_coordinate
            if cc is not None:
                col_index = int(cc.column)
                # If cursor is at the very bottom row, we are in Tailing Mode
                if current_row_count > 0 and cc.row >= current_row_count - 1:
                    is_live_edge = True
                # Otherwise, if we will evict, snapshot the anchor (Inspection Mode)
                elif will_evict and cc.row > 0 and table.has_focus:
                    anchor = (int(cc.row), int(cc.column), int(table.scroll_y))
        except Exception:
            pass

        # ── Evict the oldest row (top) if we're at capacity ─────────────
        if will_evict:
            old_key = self._intel_row_keys[0]
            try:
                table.remove_row(old_key)
            except Exception:
                pass
            self._intel_signals_by_key.pop(old_key, None)

        # ── Append new row at the bottom ────────────────────────────────
        key = f"sig-{id(sig)}"
        cells = self._signal_to_row(sig)
        try:
            table.add_row(*cells, key=key)
        except Exception:
            return
        self._intel_row_keys.append(key)
        self._intel_signals_by_key[key] = sig

        # ── Re-anchor cursor + viewport ─────────────────────────────────
        try:
            from textual.coordinate import Coordinate
            if is_live_edge:
                # V6.1.2 Live Tailing Mode: force cursor to the new bottom row
                new_row_count = len(self._intel_row_keys)
                table.cursor_coordinate = Coordinate(new_row_count - 1, col_index)
            elif anchor is not None:
                # V6.1.1 Inspection Mode: backward compensation
                old_row, col, old_scroll = anchor
                new_row    = max(0, old_row - 1)
                new_scroll = max(0, old_scroll - 1)
                table.cursor_coordinate = Coordinate(new_row, col)
                # Direct property write — DataTable's scroll_y is a
                # reactive int and accepts assignment without animation.
                table.scroll_y = new_scroll
        except Exception:
            pass

    def _intel_clear_table(self) -> None:
        """Clear the DataTable + row tracker maps. Used by /clear and the
        periodic safety-valve flush."""
        try:
            table: DataTable = self.query_one("#intel-log", DataTable)
            table.clear()
        except Exception:
            pass
        self._intel_row_keys.clear()
        self._intel_signals_by_key.clear()

    # v6.0 — per-chain colour palette for the Chain column. Mirrors
    # core.config._CHAIN_COLORS but kept local so this module stays
    # importable without a Config instance.
    _CHAIN_PALETTE: dict[str, str] = {
        "ETH":  "bright_blue",
        "BASE": "blue",
        "ARB":  "cyan",
        "POLY": "magenta",
        "BSC":  "yellow",
        "OPT":  "red",
        "SOL":  "bright_magenta",
        "BTC":  "orange1",
    }

    @staticmethod
    def _chain_color(chain: str) -> str:
        return WhaleHunterApp._CHAIN_PALETTE.get((chain or "ETH").upper(), "white")

    # V11.0 — cache the targets dict so the watchlist refresh doesn't
    # re-parse .env, re-decrypt the Vault, and re-load targets.json on
    # every UI tick. The cache is invalidated by _refresh_watchlist_status_safe
    # whenever the modal mutates the watchlist.
    _watchlist_cache: Optional[dict] = None

    def _refresh_watchlist_status(self, tactical=None, targets: Optional[dict] = None) -> None:
        """Render the Watchlist Status panel into the left tactical pane.

        Called at mount and after each target add/remove.

        V11.0 audit fix
        ---------------
        Optional ``targets`` arg short-circuits the full Config.from_env()
        rebuild. The TargetCommandCenter modal already holds a fresh
        config in memory; passing it through avoids hitting the disk
        and the AES Fernet vault on every Add/Delete keystroke. When
        called without ``targets`` (e.g. at mount time) we fall back to
        the cached dict, only loading from disk on the very first call.
        """
        if tactical is None:
            try:
                tactical = self.query_one("#tactical-signal", TacticalSignal)
            except Exception:
                return

        if targets is not None:
            # Hot path: caller already has the live dict. Update cache.
            self._watchlist_cache = dict(targets)
        elif self._watchlist_cache is not None:
            targets = self._watchlist_cache
        else:
            # Cold path (first call): load once from disk and cache.
            try:
                config = __import__("core.config", fromlist=["Config"]).Config.from_env(self._env_path)
                targets = getattr(config, "targets", {})
            except Exception:
                targets = {}
            self._watchlist_cache = dict(targets)

        from rich.table import Table as RichTable
        tbl = RichTable.grid(padding=(0, 1), expand=True)
        tbl.add_column(no_wrap=True, style="dim", ratio=1)
        tbl.add_column(no_wrap=True, ratio=3)

        if targets:
            for addr, label in targets.items():
                tbl.add_row(
                    Text("🎯", style="bright_yellow"),
                    Text(f"{label}  ", style="bold bright_yellow") +
                    Text(f"{addr[:10]}…", style="dim cyan")
                )
        else:
            tbl.add_row("", Text("  No targets loaded.\n  Press [T] to add a target.", style="dim italic"))

        count = len(targets)
        panel = Panel(
            tbl,
            title=f"[bold magenta]🎯 WATCHLIST[/bold magenta]  [{count} active]",
            title_align="left",
            border_style="bright_magenta" if count > 0 else "grey50",
            box=ROUNDED,
            padding=(0, 1),
        )
        tactical.update(panel)

    @staticmethod
    def _signal_to_row(sig: Signal) -> tuple:
        """Build the per-cell tuple for a DataTable row.

        V8.2 column layout (7 cells):
            Time | Chain | Target | Activity | Value | Gas | Direction
        """
        tx     = sig.transaction
        ts     = time.strftime("%H:%M:%S", time.localtime(sig.timestamp))
        alias  = getattr(sig, "target_alias", None)
        net    = getattr(tx, "network", "ETH")

        # Direction: OUT if target is sender, IN if target is receiver
        direction = "OUT" if alias and "OUT" in sig.label else "IN"
        dir_style = "bold red" if direction == "OUT" else "bold green"
        dir_cell  = Text(f"↓ {direction}" if direction == "OUT" else f"↑ {direction}", style=dir_style)

        # Target column — alias or truncated address
        target_cell = Text(alias or (tx.from_addr[:10] + "…"), style="bold green" if alias else "dim")

        # Activity — what kind of interaction
        cat_short = sig.category.name.replace("_", " ")[:16]
        if getattr(sig, "token_transfer", None):
            activity = f"{sig.token_transfer.symbol} Transfer"
        elif tx.value_eth > 0:
            activity = "ETH Transfer"
        elif tx.input_data and len(tx.input_data) >= 10:
            sig_prefix = tx.input_data[:10].lower()
            activity = {
                "0xa9059cbb": "ERC-20 Transfer",
                "0x23b872dd": "ERC-20 TransferFrom",
                "0x095ea7b3": "Token Approve",
            }.get(sig_prefix, f"Contract Call")
        else:
            activity = "Ping / 0 ETH"

        # Value — V11.0: never show a misleading "0.00 ETH" for token /
        # contract activity. Decode the input_data prefix and surface the
        # actual interaction kind so the operator sees what's happening
        # at a glance.
        if getattr(sig, "token_transfer", None):
            tt = sig.token_transfer
            val_str = f"{tt.amount_human:,.0f} {tt.symbol}"
            val_style = "bold bright_yellow"
        elif tx.value_eth > 0:
            val_str   = f"{tx.value_eth:,.4f} ETH"
            val_style = "white"
        elif tx.input_data and len(tx.input_data) >= 10:
            sig_prefix = tx.input_data[:10].lower()
            if sig_prefix in ("0xa9059cbb", "0x23b872dd", "0x095ea7b3"):
                # ERC-20 transfer / transferFrom / approve — undecoded
                # (no contract metadata) but still carries token value.
                val_str   = "TOKEN"
                val_style = "bold cyan"
            else:
                val_str   = "SC Call"
                val_style = "cyan"
        else:
            val_str   = "—"
            val_style = "dim"

        chain_color = WhaleHunterApp._CHAIN_PALETTE.get(net, "white")

        # Every target hit is a VIP: paint row with "bold white on dark_red" for OUT
        if direction == "OUT" and alias:
            vip = "bold white on dark_red"
            return (
                Text(ts,         style=vip),
                Text(net,        style=vip),
                Text(alias,      style=vip),
                Text(activity,   style=vip),
                Text(val_str,    style=vip),
                Text(f"{tx.gas_price_gwei:,.0f}", style=vip),
                Text(f"↓ OUT",   style=vip),
            )

        return (
            ts,
            Text(net,      style=f"bold {chain_color}"),
            target_cell,
            Text(activity, style="cyan"),
            Text(val_str,  style=val_style),
            f"{tx.gas_price_gwei:,.0f}",
            dir_cell,
        )

    # ── Click-to-inspect dispatcher ──────────────────────────────────────────

    def on_data_table_row_selected(self, event: "DataTable.RowSelected") -> None:
        """Click or [Enter] on an intel-log OR archive-table row →
        push InspectionModal.

        Guards against the InspectionListScreen DataTable: that modal owns
        its own handler and consumes the event before it bubbles here.
        Both intel-log and archive-table share row keys via the same
        _intel_signals_by_key map so the lookup is unified.
        """
        if event.data_table.id not in ("intel-log", "archive-table"):
            return
        key = str(event.row_key.value) if event.row_key else None
        if key is None:
            return
        sig = self._intel_signals_by_key.get(key)
        if sig is None:
            return
        from ui.inspect import InspectionModal
        history = self._wallet_cache.get(sig.transaction.from_addr)
        self.push_screen(InspectionModal(sig, history, self._exports_dir))

    # v6.1 — tier glyphs for the tactical HUD.
    _TIER_GLYPH: dict[str, str] = {
        "MEGA_WHALE": "🦈",
        "WHALE":      "🐋",
        "DOLPHIN":    "🐬",
        "PRAWN":      "🦐",
    }

    def _update_tactical(self, signal: Signal) -> None:
        """Render a rich.Panel HUD into the tactical-signal Static.

        Border colour is score-graded: red ≥ 90, yellow ≥ 70, cyan ≥ 50,
        dim grey otherwise. is_vip bumps to bright_red. PREDICTIVE_ALERT
        bumps to magenta. Critical alpha gets a heavy double border.

        Each row of the inner Table is conditionally styled — 0 ETH dims,
        WHALE/MEGA glow bold-yellow, the tier line carries an emoji glyph
        so it pops at a glance.
        """
        widget = self.query_one("#tactical-signal", TacticalSignal)
        widget.update(self._build_tactical_panel(signal))

    @staticmethod
    def _build_tactical_panel(signal: Signal) -> Panel:
        tx    = signal.transaction
        alias = getattr(signal, "target_alias", None)
        tier  = wallet_tier(tx.value_eth)
        glyph = "🎯" if alias else WhaleHunterApp._TIER_GLYPH.get(tier, "•")
        chain = getattr(tx, "network", "ETH")

        # ── Border colour (priority: VIP > critical > predictive > score) ──
        if getattr(signal, "is_vip", False):
            border = "bright_red"
        elif signal.category == SignalCategory.CRITICAL_ALPHA:
            border = "red"
        elif signal.category == SignalCategory.PREDICTIVE_ALERT:
            border = "magenta"
        elif signal.score >= 90:
            border = "red"
        elif signal.score >= 70:
            border = "yellow"
        elif signal.score >= 50:
            border = "cyan"
        else:
            border = "grey50"

        # ── Title — category, score, conviction tag ───────────────────────
        cat_name = signal.category.name.replace("_", " ")
        if getattr(signal, "is_vip", False):
            title = f"[bold bright_red]★ VIP[/]  ·  [bold]{cat_name}[/]  ·  {signal.score}/100"
        elif signal.category == SignalCategory.PREDICTIVE_ALERT:
            title = f"[bold magenta]◆ ORACLE[/]  ·  [bold]{cat_name}[/]  ·  {signal.score}/100"
        else:
            title = f"[bold]{cat_name}[/]  ·  {signal.score}/100"

        # ── Inner stats grid (label / value, no borders) ──────────────────
        table = Table.grid(padding=(0, 1), expand=True)
        table.add_column(style="dim", justify="right", no_wrap=True, ratio=1)
        table.add_column(no_wrap=False, ratio=2)

        # Target Alias or Tier
        if alias:
            tier_text = Text(f"{glyph}  {alias}", style="bold bright_yellow")
        elif tier in ("WHALE", "MEGA_WHALE"):
            tier_text = Text(f"{glyph}  {tier}", style="bold bright_yellow")
        elif tier == "DOLPHIN":
            tier_text = Text(f"{glyph}  {tier}", style="bold cyan")
        else:
            tier_text = Text(f"{glyph}  {tier}", style="dim")

        # Value — dim on zero, glow on whale-tier
        if getattr(signal, "token_transfer", None):
            tt = signal.token_transfer
            value_text = Text(f"{tt.amount_human:,.0f} {tt.symbol} ({chain})", style="bold bright_yellow")
        elif tx.value_eth <= 0:
            value_text = Text(f"0  {chain}", style="dim italic")
        elif tier in ("WHALE", "MEGA_WHALE"):
            value_text = Text(f"{tx.value_eth:,.4f} {chain}", style="bold bright_yellow")
        else:
            value_text = Text(f"{tx.value_eth:,.4f} {chain}", style="white")

        # Score — colour-graded, repeated inside body for at-a-glance
        if signal.score >= 95:
            score_text = Text(f"{signal.score}/100", style="bold red")
        elif signal.score >= 85:
            score_text = Text(f"{signal.score}/100", style="bold orange1")
        elif signal.score >= 70:
            score_text = Text(f"{signal.score}/100", style="gold1")
        else:
            score_text = Text(f"{signal.score}/100", style="dim")

        age = wallet_age_label(tx.nonce)

        table.add_row("Tier",    tier_text)
        table.add_row("Value",   value_text)
        table.add_row("Score",   score_text)
        table.add_row("Label",   Text(signal.label[:32], style=f"bold {border}"))
        table.add_row("Gas",     Text(f"{tx.gas_price_gwei:,.0f} gwei", style="white"))
        table.add_row("Age",     Text(age, style="dim"))
        table.add_row("Hunter",  Text(signal.hunter_id.value, style="cyan"))
        
        f_str = f"{tx.from_addr} (TARGET)" if alias and "OUT" in signal.label else tx.from_addr
        t_str = f"{tx.to_addr} (TARGET)" if alias and "IN" in signal.label else tx.to_addr
        
        table.add_row("From",    Text(f_str, style="bold cyan" if "OUT" in signal.label else "dim cyan"))
        table.add_row("To",      Text(t_str, style="bold cyan" if "IN" in signal.label else "dim cyan"))

        if signal.details:
            d = signal.details if len(signal.details) <= 110 else signal.details[:108] + "…"
            table.add_row("Detail", Text(d, style="italic dim"))

        # CRITICAL_ALPHA / score 100 — double-up the border for visual
        # priority (HEAVY box → looks like double-line frame).
        box_style = HEAVY if (signal.score >= 95 or
                              signal.category == SignalCategory.CRITICAL_ALPHA) else ROUNDED

        return Panel(
            table,
            title=title,
            title_align="left",
            border_style=border,
            box=box_style,
            padding=(0, 1),
        )

    def _update_history(self) -> None:
        # Iterate the deque (newest-last). Collect at most the last 12
        # matching the active filters — bounded work per render tick.
        recent: deque[Signal] = deque(maxlen=12)
        for s in self._signals:
            if s.score < self.display_threshold:
                continue
            if self.filter_min_eth is not None and s.transaction.value_eth < self.filter_min_eth:
                continue
            if self.filter_max_eth is not None and s.transaction.value_eth > self.filter_max_eth:
                continue
            recent.append(s)

        lines = []
        for sig in reversed(recent):
            ts   = time.strftime("%H:%M:%S", time.localtime(sig.timestamp))
            tier = wallet_tier(sig.transaction.value_eth)
            tag  = f"[{tier[0]}]"
            lines.append(f"  {ts}  {sig.score:>3}/100  {tag}  {sig.label[:16]}")
        self.query_one("#tactical-history", TacticalHistory).history_text = (
            "\n".join(lines) if lines else "  No signals yet."
        )

    # ── Metrics bar ───────────────────────────────────────────────────────────

    def _update_metrics(self) -> None:
        bar  = self.query_one("#metrics-bar", MetricsBar)
        fps  = self._monitor.fps
        avg  = self._monitor.avg_fps
        total = self._monitor.total_frames

        fill, dropped = 0.0, 0
        if self._metrics_getter:
            m       = self._metrics_getter()
            fill    = m.get("fill",       0.0)
            dropped = m.get("dropped",    0)
            # V8.5 — absorb pulse data from the ingestor process.
            pulse_ts = m.get("last_pulse", 0.0)
            if pulse_ts and pulse_ts > self._last_pulse_ts:
                self._last_pulse_ts = pulse_ts
            self._raw_count = m.get("raw_count", self._raw_count)
            self._telemetry.record_queue(fill)

        tags = ""
        if fps > _BURST_THRESHOLD:
            tags += " [BURST]"
        if self._demo_active:
            tags += " [DEMO]"
        if self._paused:
            tags += " [FROZEN]"
        if self._reconnecting:
            tags += " [RECONNECTING]"

        try:
            import ingestion.storage as storage
            if storage.DB_OFFLINE:
                tags += " [DB: OFFLINE]"
            elif not storage.DB_ENABLED:
                tags += " [DB: DISABLED]"
        except Exception:
            pass

        if self.display_threshold > 0:
            tags += f" [SCORE>={self.display_threshold}]"
        if self.filter_min_eth is not None and self.filter_max_eth is not None:
            tags += f" [FILTER: {self.filter_min_eth}-{self.filter_max_eth} ETH]"
        elif self.filter_min_eth is not None:
            tags += f" [FILTER: >={self.filter_min_eth} ETH]"

        gas_spark   = self._telemetry.gas_sparkline(width=20)
        queue_spark = self._telemetry.queue_sparkline(width=10)

        # V8.5 — WSS Pulse indicator: shows how many seconds ago the last
        # raw WSS message arrived, colour-coded for connection health.
        now = time.time()
        if self._last_pulse_ts > 0:
            pulse_age = now - self._last_pulse_ts
            if pulse_age < 5:
                pulse_indicator = f"[green]WSS ● {pulse_age:.0f}s[/green]"
            elif pulse_age < 30:
                pulse_indicator = f"[yellow]WSS ◐ {pulse_age:.0f}s[/yellow]"
            else:
                pulse_indicator = f"[red]WSS ○ {pulse_age:.0f}s![/red]"
        else:
            pulse_indicator = "[dim]WSS ··· waiting[/dim]"

        # Sovereign Terminal v4.0:
        #   • Heartbeat dot toggles every metrics tick — confirms the UI
        #     loop is alive even if FPS goes to 0 (e.g. WSS dropped).
        #   • Σ ETH is the cumulative value of all signals consumed.
        #   • Workers metric pulled from the extended metrics_getter (when
        #     run under the multiprocess Daemon).
        heartbeat = "●" if self._heartbeat else "○"

        worker_str = ""
        if self._metrics_getter:
            wa = m.get("workers_alive")
            wc = m.get("worker_count")
            if wc is not None and wa is not None:
                worker_str = f" | W: {wa}/{wc}"

        bar.metrics_text = (
            f"  {heartbeat} {pulse_indicator}  FPS: {fps:,.0f}{tags} | "
            f"Raw: {self._raw_count:,} | Sig: {self._total_signals} | "
            f"Alrt: {self._total_alerts}{worker_str} | "
            f"Gas[{gas_spark}] | "
            f"Q[{queue_spark}] {fill:.0f}% | "
            f"Σ {self._total_eth:,.0f} ETH"
        )


    # ── Heartbeat & view-aware analytics ─────────────────────────────────────

    def _toggle_heartbeat(self) -> None:
        """Flip the heartbeat reactive so the metrics bar dot blinks."""
        self._heartbeat = not self._heartbeat

    def _update_analytics_if_active(self) -> None:
        """Refresh the analytics body Static — only when its view is current."""
        if self._current_view != "analytics-view":
            return
        try:
            self._render_analytics()
        except Exception:
            pass

    def _render_analytics(self) -> None:
        """Build the analytics dashboard body content."""
        body = self.query_one("#analytics-body", Static)
        m = self._metrics_getter() if self._metrics_getter else {}

        fps  = self._monitor.fps
        avg  = self._monitor.avg_fps
        txs  = m.get("tx_count", 0)
        fill = m.get("fill", 0.0)
        ingestor_alive = m.get("ingestor_alive", False)
        ingestor_pid   = m.get("ingestor_pid")
        workers_alive  = m.get("workers_alive", 0)
        worker_count   = m.get("worker_count", 0)
        worker_pids    = m.get("worker_pids", [])

        ing_state = "[#a8d088]ALIVE[/#a8d088]" if ingestor_alive else "[#e85060]DEAD[/#e85060]"
        ing_pid   = ingestor_pid if ingestor_pid is not None else "—"

        if worker_count == 0:
            workers_line = "  [dim]No worker pool active (single-process fallback)[/dim]"
        else:
            wstate = (
                f"[#a8d088]{workers_alive}/{worker_count}[/#a8d088]"
                if workers_alive == worker_count
                else f"[#e85060]{workers_alive}/{worker_count}[/#e85060]"
            )
            pids = ", ".join(str(p) for p in worker_pids) if worker_pids else "—"
            workers_line = f"  Workers    : {wstate} alive  (PIDs: {pids})"

        body.update(
            f"[bold #c0a060]DAEMON STATE[/bold #c0a060]\n"
            f"  Ingestor   : {ing_state}  (PID {ing_pid})\n"
            f"{workers_line}\n"
            f"\n"
            f"[bold #c0a060]PIPELINE[/bold #c0a060]\n"
            f"  Paused     : {'YES' if self._paused else 'no'}\n"
            f"  Reconnect  : {'YES' if self._reconnecting else 'no'}\n"
            f"\n"
            f"[bold #c0a060]COUNTERS[/bold #c0a060]\n"
            f"  Raw TX     : {txs:,}\n"
            f"  Signals    : {self._total_signals:,}\n"
            f"  Alerts     : {self._total_alerts:,}\n"
            f"  Σ ETH      : {self._total_eth:,.2f}\n"
            f"\n"
            f"[bold #c0a060]TELEMETRY[/bold #c0a060]\n"
            f"  FPS        : {fps:,.0f}  (avg {avg:,.0f})\n"
            f"  Queue fill : {fill:.1f}%\n"
            f"\n"
            f"[bold #c0a060]ACTIVE FILTERS[/bold #c0a060]\n"
            f"  Score >=   : {self.display_threshold or '—'}\n"
            f"  Min ETH    : {self.filter_min_eth if self.filter_min_eth is not None else '—'}\n"
            f"  Max ETH    : {self.filter_max_eth if self.filter_max_eth is not None else '—'}\n"
        )

    # ── View switching (ContentSwitcher) ─────────────────────────────────────

    def _switch_view(self, view_id: str, label: str) -> None:
        """Switch the ContentSwitcher view, sync child data, then move
        focus to the primary widget of the new view.

        Focus handoff matters: without it, focus may stay on the
        previously-visible (now hidden) widget. App-level bindings still
        work because numeric keys aren't consumed by DataTable, but the
        TUI feels stuck. After this, arrow-nav inside the live DataTable
        and the next 1/2/3 keystroke both behave as expected.
        """
        try:
            switcher = self.query_one("#view-switcher", ContentSwitcher)
            if switcher.current == view_id:
                # Already on this view — refocus to recover from any
                # stuck focus state (e.g. quick-bar dismissed weirdly).
                self._focus_current_view(view_id)
                return
            switcher.current = view_id
            self._current_view = view_id
            if view_id == "archive-view":
                self._sync_archive_table()
            elif view_id == "analytics-view":
                self._render_analytics()
            self._focus_current_view(view_id)
            pass  # sub-title already reflects the active view
        except Exception:
            pass

    def _focus_current_view(self, view_id: Optional[str] = None) -> None:
        """Move focus to the primary interactive widget of the active view.

        Radar     → #intel-log (DataTable, accepts arrows + Enter).
        Archive   → #archive-table.
        Analytics → fall back to the screen so app bindings dominate.
        """
        target = {
            "radar-view":     "#intel-log",
            "archive-view":   "#archive-table",
        }.get(view_id or self._current_view)
        if target is not None:
            try:
                self.query_one(target).focus()
                return
            except Exception:
                pass
        # Analytics view (Static, not focusable) → defer to the screen so
        # numeric / letter app bindings keep firing.
        try:
            self.screen.focus()
        except Exception:
            pass

    def action_view_radar(self) -> None:
        self._switch_view("radar-view", "RADAR")

    def action_view_archive(self) -> None:
        self._switch_view("archive-view", "ARCHIVE")

    def action_view_analytics(self) -> None:
        self._switch_view("analytics-view", "ANALYTICS")

    def _sync_archive_table(self) -> None:
        """Rebuild the archive DataTable from the current _signals deque.
        Newest first. Reuses the same row keys as intel-log so the click
        handler can resolve them through _intel_signals_by_key.
        """
        try:
            table: DataTable = self.query_one("#archive-table", DataTable)
        except Exception:
            return
        table.clear()
        # Walk the row-key deque (matches _signals order) newest first.
        for key in reversed(self._intel_row_keys):
            sig = self._intel_signals_by_key.get(key)
            if sig is None:
                continue
            try:
                table.add_row(*self._signal_to_row(sig), key=key)
            except Exception:
                continue

    # ── Keybinding actions ────────────────────────────────────────────────────

    def action_toggle_pause(self) -> None:
        self._paused = not self._paused

    def _toggle_modal(self, screen_type: type, factory) -> None:
        """
        Open a modal screen, or dismiss it if it's already on top.
        Top-of-stack toggle — pressing the same hotkey twice closes the modal.
        """
        if isinstance(self.screen, screen_type):
            self.pop_screen()
            return
        self.push_screen(factory())

    def action_inspect(self) -> None:
        from ui.inspect import InspectionListScreen

        # Toggle: if archive already open, close it
        if isinstance(self.screen, InspectionListScreen):
            self.pop_screen()
            return

        # Apply active filters to the underlying data list for the DataTable
        filtered_signals = []
        for s in self._signals:
            if self.display_threshold > 0 and s.score < self.display_threshold:
                continue
            if self.filter_min_eth is not None and s.transaction.value_eth < self.filter_min_eth:
                continue
            if self.filter_max_eth is not None and s.transaction.value_eth > self.filter_max_eth:
                continue
            filtered_signals.append(s)

        self.push_screen(
            InspectionListScreen(
                signals=filtered_signals,
                wallet_cache=self._wallet_cache,
                exports_dir=self._exports_dir,
            )
        )

    def action_config(self) -> None:
        self._toggle_modal(ConfigScreen, ConfigScreen)

    def action_filter(self) -> None:
        self._toggle_modal(FilterScreen, FilterScreen)

    def action_demo(self) -> None:
        from ui.demo import DemoScreen
        if isinstance(self.screen, DemoScreen):
            self.pop_screen()
            return
        self.push_screen(DemoScreen(self._signal_queue))

    def action_help(self) -> None:
        self._toggle_modal(HelpScreen, HelpScreen)

    def action_target_center(self) -> None:
        """Mount the Target Command Center modal.

        V11.0 hot-reload contract:
          - refresh_ui_cb fires after every add/delete inside the modal,
            so the watchlist panel updates instantly while the modal is
            still open.
          - The push_screen() callback fires when the modal pops (Close
            button or Escape), giving us a final safety-net refresh in
            case anything mutated targets.json out-of-band.
        """
        from core.config import Config
        try:
            config = Config.from_env(self._env_path)
            from ui.targets import TargetCommandCenter
            modal = TargetCommandCenter(
                config,
                str(self._env_path),
                hot_reload_cb = self._hot_reload,
                refresh_ui_cb = self._refresh_watchlist_status_safe,
            )
            self.push_screen(modal, callback=lambda _result: self._refresh_watchlist_status_safe())
        except Exception as e:
            self.notify(f"Could not open Target Command Center: {e}", severity="error")

    def _refresh_watchlist_status_safe(self, targets: Optional[dict] = None) -> None:
        """Wrapper around _refresh_watchlist_status that swallows query
        errors when called from a modal callback (the tactical widget
        may not be queryable from inside a screen transition).

        V11.0 — accepts an optional ``targets`` dict so the modal can
        pass its already-loaded watchlist directly, bypassing the disk
        + Vault decrypt path and the cache."""
        try:
            self._refresh_watchlist_status(targets=targets)
        except Exception:
            pass

    def action_focus_quick_bar(self) -> None:
        def check_command(cmd: str) -> None:
            if not cmd:
                self._focus_current_view()
                return
            cmd = cmd.strip().lower()
            if cmd == ":quit":
                self.action_quit()
            elif cmd == ":clear":
                self._signals.clear()
                self.query_one("#tactical-history", TacticalHistory).history_text = "  No signals yet."
                self._intel_clear_table()
            elif cmd == ":logs":
                # Cross-platform "open exports folder" — works on Windows,
                # macOS, and Linux without crashing on any of them.
                import os, sys, subprocess
                path = str(self._exports_dir)
                try:
                    if os.name == "nt":
                        os.startfile(path)
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", path])
                    else:
                        subprocess.Popen(["xdg-open", path])
                except Exception as exc:
                    self.notify(f"Could not open exports folder: {exc}", severity="error")
            elif cmd.startswith("/filter "):
                try:
                    self.display_threshold = int(cmd.split()[1])
                    self.notify(f"Display filter set to >= {self.display_threshold} score.")
                    self._apply_filters()
                except ValueError:
                    self.notify("Invalid filter. Use /filter <number>", severity="error")
            elif cmd.startswith("/min "):
                try:
                    self.filter_min_eth = float(cmd.split()[1])
                    self.filter_max_eth = None
                    self._apply_filters()
                except ValueError:
                    self.notify("Invalid min value. Use /min <number>", severity="error")
            elif cmd.startswith("/range "):
                try:
                    parts = cmd.split()
                    self.filter_min_eth = float(parts[1])
                    self.filter_max_eth = float(parts[2])
                    self._apply_filters()
                except (ValueError, IndexError):
                    self.notify("Invalid range. Use /range <min> <max>", severity="error")
            elif cmd == "/reset":
                self.display_threshold = 0
                self.filter_min_eth = None
                self.filter_max_eth = None
                self._apply_filters()
                
            self.query_one("#tactical").focus()

        self.push_screen(CommandPaletteScreen(), check_command)

    def action_cancel_quick_bar(self) -> None:
        # Force focus back to the live view's primary widget so subsequent
        # 1/2/3 hotkeys reach the App-level bindings.
        self._focus_current_view()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        pass

    def _apply_filters(self) -> None:
        """Apply active filters to the UI without freezing (batch refresh).

        DataTable variant: clear the table + tracker maps, then walk the
        deque and re-add rows that pass the active filter set.
        """
        # Update Header label — always include network tag for multi-chain
        # awareness; filter readout overlays on the right.
        if self.filter_min_eth is not None and self.filter_max_eth is not None:
            overlay = f"FILTER: {self.filter_min_eth} - {self.filter_max_eth} ETH"
        elif self.filter_min_eth is not None:
            overlay = f"FILTER: >= {self.filter_min_eth} ETH"
        else:
            overlay = ""
        self.sub_title = self._format_subtitle(filter_overlay=overlay)

        # Update Tactical History side-panel
        self._update_history()

        # Reset the DataTable + tracker maps, then re-populate from the deque.
        self._intel_clear_table()
        intel: DataTable = self.query_one("#intel-log", DataTable)
        for s in self._signals:
            if self.display_threshold > 0 and s.score < self.display_threshold:
                continue
            if self.filter_min_eth is not None and s.transaction.value_eth < self.filter_min_eth:
                continue
            if self.filter_max_eth is not None and s.transaction.value_eth > self.filter_max_eth:
                continue
            self._append_intel_row(intel, s)

    # ── Public API (called from pipeline task) ────────────────────────────────

    def increment_alerts(self) -> None:
        self._total_alerts += 1

    def set_reconnecting(self, reconnecting: bool, attempt: int = 0) -> None:
        """Called from the pipeline task (same event loop) to update reconnect state."""
        self._reconnecting = reconnecting
        if not self.is_running:
            return
            
        if reconnecting and self._reconnect_overlay is None:
            overlay = ReconnectOverlay(
                f"  Attempt {attempt}  —  retrying with exponential backoff..."
            )
            self._reconnect_overlay = overlay
            try:
                self.push_screen(overlay)
            except Exception:
                pass
        elif reconnecting and self._reconnect_overlay is not None:
            self._reconnect_overlay.update_message(
                f"  Attempt {attempt}  —  retrying with exponential backoff..."
            )

    def notify_watchdog_resurrection(self) -> None:
        """Called thread-safely when the watchdog rescues a stalled pipeline."""
        if self.is_running:
            self.notify(
                "🚨 WATCHDOG TRIGGERED: Pipeline stalled. System resurrected.",
                title="System Alert",
                severity="error",
                timeout=10.0,
            )


# ── Rich color map ────────────────────────────────────────────────────────────

def _signal_color_rich(signal: Signal) -> str:
    # Priority 1: CRITICAL ALPHA / HIGH BAYESIAN CONVICTION (Bright Red)
    if signal.score >= 95 or signal.category == SignalCategory.CRITICAL_ALPHA:
        return "#ff0000"
    
    # Priority 2: CRIME / RUG-PULL RISK (Deep Magenta)
    if signal.category in (SignalCategory.CRIME_FLOW, SignalCategory.LIQUIDITY_MOVE):
        return "#8700af"
        
    # Priority 3: FRONT-RUN / GAS ACCELERATION (Light Cyan, high acceleration 4.0x+)
    if signal.category == SignalCategory.FRONT_RUN and signal.score >= 90:
        return "#87ffff"
        
    # Default (90% of Feed): Amber/Orange
    return "#d4a017"
