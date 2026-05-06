"""
ui/demo.py — Sovereign-Grade Interactive Demo Mode.

DemoScreen is a Textual ModalScreen that overlays the live TUI.
It runs a scripted 3-event walkthrough showcasing the new heuristics:

  Event 1  STABLECOIN_MOVE  $10M USDT transfer decoded from input_data
  Event 2  FRONT_RUN        Gas spike 8.9× above 60s rolling P90
  Event 3  CRITICAL_ALPHA   DORMANT + CRIME on same tx → Bayesian 100/100

Each event:
  1. Writes color-coded commentary to the demo log
  2. Mounts a transient tooltip widget pointing at the relevant panel
  3. Injects the synthetic Signal into the live app's signal_queue
     (the main TUI updates in the background — tactical pane + intel stream)

Press ESC to exit demo and resume the live / simulation feed.
"""

from __future__ import annotations

import asyncio
import time

from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static, RichLog, Footer
from textual.containers import Vertical, Horizontal

from core.types import (
    Transaction, Signal, SignalCategory, HunterID,
)

# ── Pre-built demo transactions ───────────────────────────

_TX_STABLECOIN = Transaction(
    tx_hash     = "0x" + "1d3m0" * 12 + "1111",
    from_addr   = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045",
    to_addr     = "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
    value_wei   = 0,
    value_eth   = 0.0,
    gas_price_gwei = 22.0,
    nonce       = 3,
    input_data  = (
        "0xa9059cbb"
        + "000000000000000000000000"
        + "abcdef1234567890abcdef1234567890abcdef12"
        + format(10_000_000 * 10 ** 6, "064x")
    ),
)

_TX_FRONTRUN = Transaction(
    tx_hash     = "0x" + "2d3m0" * 12 + "2222",
    from_addr   = "0x" + "fe" * 20,
    to_addr     = "0x" + "ab" * 20,
    value_wei   = int(5 * 1e18),
    value_eth   = 5.0,
    gas_price_gwei = 195.0,
    nonce       = 1,
    input_data  = "0x",
)

_TX_CRITICAL = Transaction(
    tx_hash     = "0x" + "3d3m0" * 12 + "3333",
    from_addr   = "0xde0b295669a9fd93d5f28d9ec85e40f4cb697bae",  # Known dormant
    to_addr     = "0xa160cdab225685da1d56aa342ad8841c3b53f291",  # Tornado 100 ETH
    value_wei   = int(500 * 1e18),
    value_eth   = 500.0,
    gas_price_gwei = 28.0,
    nonce       = 1,
    input_data  = "0x",
)

# ── Pre-built demo signals ────────────────────────────────

_SIG_STABLECOIN = Signal(
    category   = SignalCategory.TOKEN_TRANSFER,
    hunter_id  = HunterID.STABLECOIN,
    score      = 92,
    transaction = _TX_STABLECOIN,
    label      = "STABLECOIN MEGA WHALE | USDT",
    details    = "USDT: 10,000,000.00 ($10,000,000) | Contract: 0xdac17f958d...",
    timestamp  = time.time(),
)

_SIG_FRONTRUN = Signal(
    category   = SignalCategory.FRONT_RUN,
    hunter_id  = HunterID.FRONTRUN,
    score      = 90,
    transaction = _TX_FRONTRUN,
    label      = "FRONT-RUN DETECTED",
    details    = "Gas: 195 gwei | P90(60s): 22 gwei | Ratio: 8.9x | Samples: 60",
    timestamp  = time.time(),
)

_SIG_CRITICAL = Signal(
    category   = SignalCategory.CRITICAL_ALPHA,
    hunter_id  = HunterID.CORRELATOR,
    score      = 100,
    transaction = _TX_CRITICAL,
    label      = "[!!] CRITICAL ALPHA",
    details    = (
        "Bayesian: 100/100 | ABSOLUTE conviction | "
        "DORMANT WAKING + MIXER on same tx"
    ),
    timestamp  = time.time(),
)

# ── CSS ───────────────────────────────────────────────────

_CSS = """
DemoScreen {
    align: center middle;
}

#demo-outer {
    width: 92;
    height: 38;
    border: solid #ff8c00;
    background: #080808;
    padding: 0 1;
}

#demo-title {
    height: 3;
    content-align: center middle;
    background: #1a0800;
    color: #ff8c00;
    border-bottom: solid #ff8c00;
    text-style: bold;
}

#panel-guide {
    height: 8;
    border-bottom: dashed #3a2800;
    padding: 1 0;
    color: #8a6020;
}

#demo-log {
    height: 1fr;
    padding: 0 1;
}

#demo-status {
    height: 2;
    border-top: solid #ff8c00;
    content-align: center middle;
    color: #d4a017;
    background: #0f0800;
}
"""

# ── Panel guide text (displayed at top of demo screen) ───

_GUIDE = (
    "  ┌────────────────────────────┐    ┌──────────────────────────────────────┐\n"
    "  │  TACTICAL PANE  (left col) │    │  INTEL STREAM          (right panel) │\n"
    "  │  • Signal ASCII art        │    │  • Full tx log per signal            │\n"
    "  │  • Score / Tier / Age      │    │  • ETH value / gas / addresses       │\n"
    "  │  • Last-10 signal history  │    │  • Color-coded by signal category    │\n"
    "  └────────────────────────────┘    └──────────────────────────────────────┘"
)

# ── Script steps ──────────────────────────────────────────

_STEPS: list[tuple[str, str, Signal]] = [
    (
        "[gold1]EVENT 1 — STABLECOIN MEGA WHALE[/gold1]",
        (
            "  StablecoinHunter decoded a [gold1]transfer(address,uint256)[/gold1] call\n"
            "  to the USDT contract (0xdac17f95...) from [gold1]input_data[/gold1].\n"
            "  Amount: [bold]$10,000,000 USDT[/bold]  Score: 92/100  Tier: [WHALE]\n"
            "  → Watch the TACTICAL PANE update with TOKEN TRANSFER art.\n"
            "  → The INTEL STREAM logs the full tx with a [gold1]gold[/gold1] highlight."
        ),
        _SIG_STABLECOIN,
    ),
    (
        "[orange_red1]EVENT 2 — FRONT-RUN / MEV DETECTED[/orange_red1]",
        (
            "  FrontRunDetector maintains a 60-second rolling gas window.\n"
            "  This tx's gas price: [bold]195 gwei[/bold]  |  P90 of last 60s: 22 gwei\n"
            "  Ratio: [bold]8.9×[/bold] above P90  →  Score: 90/100  (near-certain MEV)\n"
            "  → TACTICAL PANE shows FRONT-RUN art and [DOLPHIN] tier.\n"
            "  → Look for the [orange_red1]orange-red[/orange_red1] line in INTEL STREAM."
        ),
        _SIG_FRONTRUN,
    ),
    (
        "[bold red]EVENT 3 — CRITICAL ALPHA  100/100[/bold red]",
        (
            "  Same transaction triggered BOTH DormantHunter AND CrimeWatch:\n"
            "    • From: [yellow]0xde0b2956...[/yellow] (Genesis-era dormant wallet)\n"
            "    • To:   [red]0xa160cdab...[/red] (Tornado Cash 100 ETH pool)\n"
            "  Bayesian correlator: max(1.20×90, 1.15×90) + 10 = [bold red]100/100[/bold red]\n"
            "  Conviction: [bold red]ABSOLUTE[/bold red] — Telegram alert dispatched.\n"
            "  → TACTICAL PANE shows CRITICAL ALPHA art and !! banner.\n"
            "  → INTEL STREAM highlights in [bold red]bold red[/bold red]."
        ),
        _SIG_CRITICAL,
    ),
]

_DELAY_INTRO_SEC = 1.5
_DELAY_STEP_SEC  = 5.0   # Pause between scripted events
_DELAY_OUTRO_SEC = 2.0


# ── DemoScreen ────────────────────────────────────────────

class DemoScreen(ModalScreen):
    """
    Full-screen interactive demo overlay.

    Accepts the app's signal_queue so scripted events appear
    live in the underlying TUI tactical pane and intel stream.
    """

    CSS = _CSS
    BINDINGS = [("escape", "dismiss", "Exit Demo")]

    def __init__(self, signal_queue: asyncio.Queue) -> None:
        super().__init__()
        self._queue = signal_queue
        self._step  = 0

    # ── Layout ───────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical(id="demo-outer"):
            yield Static(
                "  WHALE HUNTER v11.0 — SOVEREIGN DEMO MODE  |  ESC to exit",
                id="demo-title",
            )
            yield Static(_GUIDE, id="panel-guide")
            yield RichLog(
                id="demo-log",
                highlight=True,
                markup=True,
                max_lines=200,
            )
            yield Static("  Initializing scripted walkthrough...", id="demo-status")

    # ── Lifecycle ─────────────────────────────────────────

    def on_mount(self) -> None:
        # Pause live pipeline signals from reaching the TUI while demo runs
        if hasattr(self.app, "_demo_active"):
            self.app._demo_active = True
        self.run_worker(self._run_sequence, exclusive=True, thread=False)

    def on_unmount(self) -> None:
        if hasattr(self.app, "_demo_active"):
            self.app._demo_active = False

    # ── Scripted sequence ─────────────────────────────────

    async def _run_sequence(self) -> None:
        log: RichLog   = self.query_one("#demo-log")
        status: Static = self.query_one("#demo-status")

        await asyncio.sleep(_DELAY_INTRO_SEC)
        log.write(
            "[orange3]┌───────────────────────────────────────────────────────┐[/orange3]\n"
            "[orange3]│  SCRIPTED WALKTHROUGH — 3 Events                      │[/orange3]\n"
            "[orange3]│  Each event injects a real Signal into the live TUI.  │[/orange3]\n"
            "[orange3]└───────────────────────────────────────────────────────┘[/orange3]\n"
        )

        for i, (title, commentary, signal) in enumerate(_STEPS, start=1):
            self._step = i
            status.update(f"  Step {i}/3 — {title}  |  ESC to exit")

            # Write step header
            log.write(f"\n[dim]{'─' * 57}[/dim]")
            log.write(f"  {title}")
            log.write(f"[dim]{'─' * 57}[/dim]")
            log.write(commentary)

            # Mount a transient tooltip callout on this screen
            tooltip = self._make_tooltip(i, signal)
            await self.mount(tooltip)

            # Inject signal into live TUI (non-blocking)
            signal.timestamp = time.time()   # freshen timestamp
            try:
                self._queue.put_nowait(signal)
            except asyncio.QueueFull:
                pass

            log.write(
                f"\n  [dim]→ Signal injected into live feed."
                f" Watch the background TUI update.[/dim]"
            )

            await asyncio.sleep(_DELAY_STEP_SEC)
            # Remove tooltip before next step
            try:
                tooltip.remove()
            except Exception:
                pass

        # Outro
        await asyncio.sleep(_DELAY_OUTRO_SEC)
        log.write(
            "\n[orange3]┌───────────────────────────────────────────────────────┐[/orange3]\n"
            "[orange3]│  DEMO COMPLETE                                         │[/orange3]\n"
            "[orange3]│  All 3 signal types demonstrated successfully.         │[/orange3]\n"
            "[orange3]│  Press ESC to return to live intelligence feed.        │[/orange3]\n"
            "[orange3]└───────────────────────────────────────────────────────┘[/orange3]"
        )
        status.update(
            "  Demo complete — Press [ESC] to resume live feed"
        )

    # ── Tooltip widget factory ────────────────────────────

    def _make_tooltip(self, step: int, signal: Signal) -> Static:
        """Build a callout Static widget for a demo step."""
        panel_labels = {
            1: "← STABLECOIN detection via input_data decode  |  Watch TACTICAL PANE →",
            2: "← Gas velocity anomaly flagged by FrontRunDetector  |  INTEL STREAM →",
            3: "← DORMANT + CRIME fired simultaneously  |  CRITICAL ALPHA escalated →",
        }
        label = panel_labels.get(step, "")
        widget = Static(
            f"  [bold orange1]{label}[/bold orange1]",
            id=f"tooltip-step-{step}",
        )
        widget.styles.height   = 1
        widget.styles.dock     = "top"
        widget.styles.background = "#1a0800"
        return widget

    def action_dismiss(self) -> None:
        self.dismiss()
