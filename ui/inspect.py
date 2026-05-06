"""
ui/inspect.py — Signal Inspection System  (Whale Hunter v3.0, Phase 3)

InspectionListScreen : DataTable of all buffered signals  →  [Enter] to drill
InspectionModal      : Full detail view — Bayesian breakdown, wallet history,
                       [S] export to exports/ directory as JSON,
                       v4.1 OSINT enrichment + external explorer launch.
"""
from __future__ import annotations

import asyncio
import json
import time
import webbrowser
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, RichLog, Static

from core.intelligence import HUNTER_WEIGHTS, conviction_label
from core.types import Signal, wallet_tier

# v4.2 — chain-aware URL helpers were hoisted to utils/explorer.py so
# non-UI callers (alerts/telegram.py) can use them without dragging in
# Textual. Re-imported here for back-compat with code that still calls
# them via ui.inspect (and to keep the InspectionModal body short).
from utils.explorer import explorer_url, arkham_url, debank_url


async def fetch_wallet_osint(address: str, chain: str = "ETH") -> dict:
    """Placeholder for future OSINT enrichment.

    Returns a dict shaped like:
        balance_native: float | None    — native token balance
        balance_usd:    float | None    — USD equivalent
        tx_count:       int   | None    — historical tx count
        tag:            str   | None    — primary label (e.g. "Binance Hot")
        first_seen:     str   | None    — ISO date of first activity
        labels:         list[str]       — local known-address tags

    Local enrichment runs immediately (zero network) by joining against
    the known-address sets in utils/addresses.py.

    To wire live Etherscan enrichment:
        1. Register a free key at etherscan.io/apis
        2. Set ETHERSCAN_API_KEY=... in .env
        3. Replace the body below with an httpx.AsyncClient call:

            async with httpx.AsyncClient(timeout=4.0) as c:
                r = await c.get(
                    "https://api.etherscan.io/api",
                    params={
                        "module": "account",
                        "action": "balance",
                        "address": address,
                        "apikey": os.environ["ETHERSCAN_API_KEY"],
                    },
                )
                balance_wei = int(r.json().get("result", "0"))
                balance_eth = balance_wei / 1e18
                ...
    """
    try:
        from utils.addresses import (
            DORMANT_WALLETS, EXCHANGE_WALLETS, GOVERNMENT_WALLETS,
            TORNADO_CONTRACTS, HACK_ADDRESSES,
        )
    except ImportError:
        DORMANT_WALLETS = EXCHANGE_WALLETS = GOVERNMENT_WALLETS = set()
        TORNADO_CONTRACTS = HACK_ADDRESSES = set()

    addr = address.lower()
    labels: list[str] = []
    if addr in DORMANT_WALLETS:    labels.append("DORMANT")
    if addr in EXCHANGE_WALLETS:   labels.append("EXCHANGE")
    if addr in GOVERNMENT_WALLETS: labels.append("GOVERNMENT")
    if addr in TORNADO_CONTRACTS:  labels.append("TORNADO_MIXER")
    if addr in HACK_ADDRESSES:     labels.append("HACK_ADDRESS")

    return {
        "balance_native": None,
        "balance_usd":    None,
        "tx_count":       None,
        "tag":            labels[0] if labels else None,
        "first_seen":     None,
        "labels":         labels,
        "chain":          chain.upper(),
    }


# ── Shared CSS ────────────────────────────────────────────────────────────────

_LIST_CSS = """
InspectionListScreen {
    align: center middle;
}

#list-outer {
    width: 96%;
    height: 85%;
    border: double #ff8c00;
    background: #080808;
}

#list-title {
    height: 3;
    content-align: center middle;
    background: #1a0800;
    color: #ff8c00;
    text-style: bold;
    border-bottom: solid #ff8c00;
}

DataTable {
    height: 1fr;
    background: #050505;
    color: #d4a017;
}

DataTable > .datatable--header {
    background: #1a0800;
    color: #ff8c00;
    text-style: bold;
}

DataTable > .datatable--cursor {
    background: #2a1400;
    color: #ffcc00;
    text-style: bold;
}

#list-hint {
    height: 2;
    content-align: center middle;
    color: #5a4820;
    border-top: dashed #3a2800;
    background: #0a0800;
}
"""

_MODAL_CSS = """
InspectionModal {
    align: center middle;
}

#modal-outer {
    width: 93%;
    height: 90%;
    border: double #ff8c00;
    background: #080808;
}

#modal-title {
    height: 3;
    content-align: center middle;
    background: #1a0800;
    color: #ff8c00;
    text-style: bold;
    border-bottom: solid #ff8c00;
}

#modal-body {
    height: 1fr;
    padding: 1 2;
}

#modal-footer {
    height: 3;
    border-top: solid #3a2800;
    background: #0a0800;
    padding: 0 1;
}

#save-btn {
    background: #001a00;
    color: #00cc60;
    border: solid #00cc60;
    width: 28;
}

#save-btn:hover {
    background: #002a00;
    color: #00ff80;
}

#close-btn {
    background: #1a0800;
    color: #8a6020;
    border: solid #3a2800;
    width: 18;
}

#save-status {
    width: 1fr;
    content-align: left middle;
    color: #4a4030;
    padding: 0 1;
}
"""


# ── InspectionListScreen ──────────────────────────────────────────────────────

class InspectionListScreen(ModalScreen):
    """DataTable of recent signals. Navigate with arrows, Enter to inspect."""

    CSS = _LIST_CSS
    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("q",      "dismiss", "Close"),
    ]

    def __init__(
        self,
        signals: list[Signal],
        wallet_cache: "WalletHistoryCache",  # noqa: F821
        exports_dir: Path,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._signals      = list(reversed(signals))   # newest first
        self._wallet_cache = wallet_cache
        self._exports_dir  = exports_dir

    def compose(self) -> ComposeResult:
        n = len(self._signals)
        with Vertical(id="list-outer"):
            yield Static(
                f"  SIGNAL ARCHIVE  —  {n} buffered signals"
                f"  |  [Enter] Inspect  [ESC] Close",
                id="list-title",
            )
            yield DataTable(id="sig-table", zebra_stripes=True, cursor_type="row")
            yield Static(
                "  [↑/↓] Navigate  |  [Enter] Open detail  |  [ESC] Return to radar",
                id="list-hint",
            )

    def on_mount(self) -> None:
        tbl: DataTable = self.query_one("#sig-table")
        tbl.add_columns(
            "Time", "Score", "Tier", "Conviction", "Category", "Label", "ETH", "From"
        )
        for sig in self._signals:
            ts         = time.strftime("%H:%M:%S", time.localtime(sig.timestamp))
            tier       = wallet_tier(sig.transaction.value_eth)
            conviction = conviction_label(sig.score)
            tbl.add_row(
                ts,
                f"{sig.score:>3}/100",
                tier,
                conviction,
                sig.category.name,
                sig.label[:26],
                f"{sig.transaction.value_eth:>10,.2f}",
                sig.transaction.from_addr[:16] + "...",
                key=str(id(sig)),
            )
        if self._signals:
            tbl.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row_key = str(event.row_key.value)
        for sig in self._signals:
            if str(id(sig)) == row_key:
                history = self._wallet_cache.get(sig.transaction.from_addr)
                self.app.push_screen(
                    InspectionModal(sig, history, self._exports_dir)
                )
                return

    def action_dismiss(self) -> None:
        self.dismiss()


# ── InspectionModal ───────────────────────────────────────────────────────────

class InspectionModal(ModalScreen):
    """Full-screen signal detail: raw tx, Bayesian breakdown, wallet history, export."""

    CSS = _MODAL_CSS
    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("q",      "dismiss", "Close"),
        Binding("s",      "save",    "Save to exports/"),
        Binding("e",      "open_etherscan", "Etherscan"),
        Binding("a",      "open_arkham",    "Arkham"),
        Binding("b",      "open_debank",    "DeBank"),
    ]

    def __init__(
        self,
        signal: Signal,
        wallet_history: list[Signal],
        exports_dir: Path,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._signal         = signal
        self._wallet_history = list(wallet_history)
        self._exports_dir    = exports_dir
        self._saved          = False

    def compose(self) -> ComposeResult:
        sig = self._signal
        title = (
            f"  WHALE HUNTER INTEL  |  {sig.label}"
            f"  |  {sig.score}/100  |  {conviction_label(sig.score)}"
        )
        with Vertical(id="modal-outer"):
            yield Static(title, id="modal-title")
            yield RichLog(
                id="modal-body",
                highlight=True,
                markup=True,
                max_lines=600,
            )
            # v4.1 — OSINT external launchers + Save / Close.
            with Horizontal(id="modal-footer"):
                yield Button("[S] Save",       id="save-btn",   variant="success")
                yield Button("[E] Etherscan",  id="ext-eth",    variant="default")
                yield Button("[A] Arkham",     id="ext-arkham", variant="default")
                yield Button("[B] DeBank",     id="ext-debank", variant="default")
                yield Static("", id="save-status")
                yield Button("[ESC] Close",    id="close-btn",  variant="primary")

    def on_mount(self) -> None:
        body = self.query_one("#modal-body", RichLog)
        self._render_report(body)
        # v4.1 — kick off OSINT enrichment asynchronously so it does not
        # block the modal mount. Local known-address tagging returns
        # instantly; future Etherscan API calls will yield to the loop.
        asyncio.create_task(self._render_osint(body))

    def _render_report(self, log: RichLog) -> None:
        tx  = self._signal.transaction
        sig = self._signal
        w   = HUNTER_WEIGHTS.get(sig.hunter_id, 1.0)

        # ── Raw transaction ──────────────────────────────────────────────────
        log.write("[bold #ff8c00]═══ RAW TRANSACTION ════════════════════════════════════════[/bold #ff8c00]")
        log.write(f"  [#d4a017]Hash      :[/#d4a017]  {tx.tx_hash}")
        log.write(f"  [#d4a017]From      :[/#d4a017]  {tx.from_addr}")
        log.write(f"  [#d4a017]To        :[/#d4a017]  {tx.to_addr}")
        log.write(
            f"  [#d4a017]Value     :[/#d4a017]  {tx.value_eth:,.6f} ETH"
            f"  ({tx.value_wei:,} wei)"
        )
        log.write(f"  [#d4a017]Gas Price :[/#d4a017]  {tx.gas_price_gwei:,.2f} gwei")
        log.write(f"  [#d4a017]Nonce     :[/#d4a017]  {tx.nonce}")
        truncated = tx.input_data[:80] + ("..." if len(tx.input_data) > 80 else "")
        log.write(f"  [#d4a017]Input     :[/#d4a017]  {truncated}")
        ts_human = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(tx.timestamp))
        log.write(f"  [#d4a017]Timestamp :[/#d4a017]  {ts_human}")

        # ── Bayesian intelligence ────────────────────────────────────────────
        log.write("")
        log.write("[bold #ff8c00]═══ BAYESIAN INTELLIGENCE REPORT ════════════════════════════[/bold #ff8c00]")
        log.write(f"  [#d4a017]Category  :[/#d4a017]  {sig.category.name}")
        log.write(f"  [#d4a017]Hunter    :[/#d4a017]  {sig.hunter_id.value}")
        log.write(f"  [#d4a017]Score     :[/#d4a017]  [bold]{sig.score}/100[/bold]")
        log.write(f"  [#d4a017]Conviction:[/#d4a017]  [bold]{conviction_label(sig.score)}[/bold]")
        log.write(f"  [#d4a017]Weight    :[/#d4a017]  {w:.2f}x  (domain reliability multiplier)")
        log.write(
            f"  [#d4a017]Weighted S:[/#d4a017]  {min(100, int(sig.score * w))}/100"
            f"  (pre-correlation peak)"
        )
        alert_txt = (
            "YES — dispatched to Telegram" if sig.is_high_confidence
            else "NO  (score < 80 threshold)"
        )
        log.write(f"  [#d4a017]Alert     :[/#d4a017]  {alert_txt}")

        # ── Hunter details ───────────────────────────────────────────────────
        if sig.details:
            log.write("")
            log.write("[bold #ff8c00]═══ HUNTER DETAILS ══════════════════════════════════════════[/bold #ff8c00]")
            for part in sig.details.split(" | "):
                log.write(f"  {part.strip()}")

        # ── Wallet history ───────────────────────────────────────────────────
        log.write("")
        log.write("[bold #ff8c00]═══ WALLET HISTORY  (last 5 cached movements) ════════════════[/bold #ff8c00]")
        if not self._wallet_history:
            log.write("  [dim]No prior movements cached for this address in this session.[/dim]")
        else:
            for i, h in enumerate(self._wallet_history[-5:], 1):
                h_ts = time.strftime("%H:%M:%S", time.localtime(h.timestamp))
                log.write(
                    f"  [{i}]  [{h_ts}]  {h.label:<32}"
                    f"  Score: {h.score:>3}/100"
                    f"  ETH: {h.transaction.value_eth:>12,.4f}"
                )

    # ── Button / action handlers ─────────────────────────────────────────────

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "save-btn":
            await self.action_save()
        elif bid == "close-btn":
            self.dismiss()
        elif bid == "ext-eth":
            self.action_open_etherscan()
        elif bid == "ext-arkham":
            self.action_open_arkham()
        elif bid == "ext-debank":
            self.action_open_debank()

    # ── External OSINT launchers (open in OS default browser) ────────────────

    def _open_url(self, url: str, label: str) -> None:
        try:
            webbrowser.open(url, new=2, autoraise=True)
            self.app.notify(f"Launched {label} → {url[:60]}…", title="OSINT", timeout=4.0)
        except Exception as exc:
            self.app.notify(f"Failed to launch {label}: {exc}", severity="error")

    def action_open_etherscan(self) -> None:
        tx = self._signal.transaction
        chain = getattr(tx, "network", "ETH")
        # Open the SENDER address — it's the entity worth investigating.
        self._open_url(explorer_url(chain, tx.from_addr, "address"), "Etherscan")

    def action_open_arkham(self) -> None:
        addr = self._signal.transaction.from_addr
        self._open_url(arkham_url(addr), "Arkham")

    def action_open_debank(self) -> None:
        addr = self._signal.transaction.from_addr
        self._open_url(debank_url(addr), "DeBank")

    # ── OSINT enrichment (entity profile section, lazy) ──────────────────────

    async def _render_osint(self, log: RichLog) -> None:
        """Fetch local + (future) remote OSINT and append an Entity Profile
        section to the report. Runs after on_mount so the modal opens
        instantly and OSINT trickles in."""
        tx = self._signal.transaction
        chain = getattr(tx, "network", "ETH")
        try:
            osint = await fetch_wallet_osint(tx.from_addr, chain=chain)
        except Exception:
            return

        log.write("")
        log.write("[bold #c0a060]═══ ENTITY PROFILE  (v4.1 OSINT) ═════════════════════════════[/bold #c0a060]")
        log.write(f"  [#8a929e]Chain     :[/#8a929e]  {osint.get('chain', chain)}")

        labels = osint.get("labels") or []
        if labels:
            tag_line = "  ".join(f"[bold #e8a040][{l}][/bold #e8a040]" for l in labels)
            log.write(f"  [#8a929e]Tags      :[/#8a929e]  {tag_line}")
        else:
            log.write("  [#8a929e]Tags      :[/#8a929e]  [dim]none in local intel set[/dim]")

        primary = osint.get("tag")
        if primary:
            log.write(f"  [#8a929e]Primary   :[/#8a929e]  [bold #e8a040]{primary}[/bold #e8a040]")

        bal = osint.get("balance_native")
        log.write(
            f"  [#8a929e]Balance   :[/#8a929e]  "
            + (f"{bal:,.4f} {chain}" if bal is not None
               else "[dim]not enriched (set ETHERSCAN_API_KEY to enable)[/dim]")
        )
        txc = osint.get("tx_count")
        log.write(
            f"  [#8a929e]TX count  :[/#8a929e]  "
            + (f"{txc:,}" if txc is not None else "[dim]—[/dim]")
        )

        log.write("")
        log.write("[bold #c0a060]═══ EXTERNAL INTEL ═══════════════════════════════════════════[/bold #c0a060]")
        log.write(f"  [#8a929e]Etherscan :[/#8a929e]  {explorer_url(chain, tx.from_addr, 'address')}")
        log.write(f"  [#8a929e]TX detail :[/#8a929e]  {explorer_url(chain, tx.tx_hash, 'tx')}")
        log.write(f"  [#8a929e]Arkham    :[/#8a929e]  {arkham_url(tx.from_addr)}")
        log.write(f"  [#8a929e]DeBank    :[/#8a929e]  {debank_url(tx.from_addr)}")
        log.write("  [dim][E]/[A]/[B] keys or footer buttons launch in your default browser[/dim]")

    async def action_save(self) -> None:
        if self._saved:
            return
        self._saved = True
        tx  = self._signal.transaction
        sig = self._signal

        self._exports_dir.mkdir(parents=True, exist_ok=True)
        ts_str   = time.strftime("%Y%m%d_%H%M%S", time.localtime(sig.timestamp))
        filename = f"signal_{ts_str}_{sig.score}_{sig.category.name}.json"
        out_path = self._exports_dir / filename

        payload = {
            "meta": {
                "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "whale_hunter_version": "3.0",
            },
            "signal": {
                "category": sig.category.name,
                "hunter_id": sig.hunter_id.value,
                "score": sig.score,
                "conviction": conviction_label(sig.score),
                "label": sig.label,
                "details": sig.details,
                "is_high_confidence": sig.is_high_confidence,
                "timestamp": sig.timestamp,
                "timestamp_human": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(sig.timestamp)
                ),
            },
            "transaction": {
                "tx_hash": tx.tx_hash,
                "from_addr": tx.from_addr,
                "to_addr": tx.to_addr,
                "value_eth": tx.value_eth,
                "value_wei": tx.value_wei,
                "gas_price_gwei": tx.gas_price_gwei,
                "nonce": tx.nonce,
                "input_data": tx.input_data,
            },
            "wallet_history": [
                {
                    "timestamp_human": time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(h.timestamp)
                    ),
                    "label":     h.label,
                    "score":     h.score,
                    "category":  h.category.name,
                    "value_eth": h.transaction.value_eth,
                }
                for h in self._wallet_history[-5:]
            ],
        }

        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.query_one("#save-status", Static).update(
            f"[#00ff80]  Saved → exports/{filename}[/#00ff80]"
        )

    def action_dismiss(self) -> None:
        self.dismiss()
