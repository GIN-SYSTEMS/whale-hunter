# Copyright (C) 2026 Whale Hunter Contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""
ui/setup.py — Whale Hunter v11.0 Sovereign Initialization Wizard

Captures operator credentials, encrypts the sensitive ones via the local
SecurityVault, and emits a fully-formed .env so the main sentinel can
boot on the next launch.

UX contract:
  * Single "Save & Encrypt to Vault" action (warning-variant button).
  * HTTPS endpoints are silently rewritten to WSS, so the operator can
    paste whichever scheme the provider dashboard handed them.
  * Blank WSS = SIMULATOR mode (no live ingestion, no API key required).

Return contract (consumed by main.run_setup):
  "live"  — WSS configured, env written.
  "sim"   — WSS blank, env written in simulator mode.
  None    — operator hit Esc, no changes persisted.
"""
from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Horizontal
from textual.widgets import Header, Footer, Input, Button, Static

from core.config import Config


_CSS = """
SetupApp {
    background: #0a0a0a;
    color: #d4a017;
}

#outer {
    align: center middle;
    width: 100%;
    height: 100%;
}

#box {
    width: 76;
    height: auto;
    border: double #ff8c00;
    background: #0f0f0f;
    padding: 1 2;
}

#title {
    height: 3;
    content-align: center middle;
    background: #1a0800;
    color: #ff8c00;
    text-style: bold;
    border-bottom: solid #ff8c00;
    margin-bottom: 1;
}

#subtitle {
    height: 1;
    color: #8a7040;
    margin-bottom: 1;
    content-align: center middle;
}

.lbl {
    height: 1;
    color: #d4a017;
    margin-top: 1;
    text-style: bold;
}

Input {
    border: solid #3a2800;
    background: #050505;
    color: #ff8c00;
    height: 3;
}

Input:focus {
    border: solid #ff8c00;
}

#btn-row {
    margin-top: 2;
    height: 3;
}

#save-btn {
    width: 1fr;
}

#hint {
    color: #4a4030;
    height: 1;
    margin-top: 1;
    content-align: center middle;
}

#status {
    height: 2;
    margin-top: 1;
    color: #8a7040;
}
"""


class SetupApp(App):
    """Whale Hunter v11.0 — Sovereign Initialization.

    Three inputs, one button, vault-encrypted output. Returns "live" /
    "sim" / None to the calling main.run_setup so it knows which boot
    path to take next.
    """

    TITLE     = "WHALE HUNTER v11.0 — SOVEREIGN  ·  INITIALIZATION"
    SUB_TITLE = "Sovereign Setup Wizard"
    CSS       = _CSS
    BINDINGS  = [Binding("escape", "abort", "Abort")]

    def __init__(self, env_path: Path, **kwargs):
        super().__init__(**kwargs)
        self._env_path = env_path
        self._saving   = False

    # ── Layout ─────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="outer"):
            with Vertical(id="box"):
                yield Static(
                    "  WHALE HUNTER v11.0 — SOVEREIGN  ·  INITIALIZATION",
                    id="title",
                )
                yield Static(
                    "Credentials are encrypted at rest via the local SecurityVault.",
                    id="subtitle",
                )

                yield Static("  Alchemy / Provider WSS URL:", classes="lbl")
                yield Input(
                    placeholder="wss:// or https:// — auto-converted to wss://",
                    id="wss-input",
                )

                yield Static("  Telegram Bot Token:", classes="lbl")
                yield Input(
                    placeholder="leave blank to disable Telegram",
                    id="tg-token-input",
                    password=True,
                )

                yield Static("  Telegram Chat ID:", classes="lbl")
                yield Input(
                    placeholder="numeric (e.g. 123456789 or -100123456789 for groups)",
                    id="tg-chat-input",
                )

                with Horizontal(id="btn-row"):
                    yield Button(
                        "Save & Encrypt to Vault",
                        variant="warning",
                        id="save-btn",
                    )

                yield Static(
                    "Leave WSS blank for SIMULATOR mode  ·  [ESC] to abort",
                    id="hint",
                )
                yield Static("", id="status")
        yield Footer()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        """Pre-fill inputs from an existing .env when re-running --setup."""
        try:
            src = self._env_path if self._env_path.exists() else None
            cfg = Config.from_env(src)
            if cfg.wss_url:
                self.query_one("#wss-input", Input).value = cfg.wss_url
            if cfg.telegram_bot_token:
                self.query_one("#tg-token-input", Input).value = cfg.telegram_bot_token
            if cfg.telegram_chat_id:
                self.query_one("#tg-chat-input", Input).value = cfg.telegram_chat_id
        except Exception:
            # Fresh install or corrupt env — start with blank inputs.
            pass
        # Pre-focus the WSS field so the operator can paste immediately.
        self.call_after_refresh(
            lambda: self.query_one("#wss-input", Input).focus()
        )

    # ── Input dispatch ─────────────────────────────────────────────────────

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-btn":
            await self._do_save()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        # Pressing Enter in any field commits the form. Operators rarely
        # need to keep editing once they've reached an Enter — saves a
        # mouse trip to the Save button.
        await self._do_save()

    def action_abort(self) -> None:
        self.exit(None)

    # ── Save flow ──────────────────────────────────────────────────────────

    async def _do_save(self) -> None:
        if self._saving:
            return
        self._saving = True
        btn = self.query_one("#save-btn", Button)
        original_label = btn.label
        btn.label = "Encrypting…"

        wss_raw = self.query_one("#wss-input",      Input).value.strip()
        tg_tok  = self.query_one("#tg-token-input", Input).value.strip()
        tg_chat = self.query_one("#tg-chat-input",  Input).value.strip()

        # ── HTTPS → WSS auto-conversion ─────────────────────────────
        # Provider dashboards (Alchemy, Infura, QuickNode) often copy
        # the HTTPS endpoint by default. Silently rewrite the scheme so
        # the operator can paste whichever URL their provider gave them.
        wss_url = wss_raw
        scheme_converted = False
        if wss_url.startswith("https://"):
            wss_url = "wss://" + wss_url[len("https://"):]
            scheme_converted = True
        elif wss_url.startswith("http://"):
            wss_url = "ws://" + wss_url[len("http://"):]
            scheme_converted = True

        # Operator left WSS blank → simulator mode (offline synthetic feed).
        sim = not wss_url

        try:
            self._write_env(
                wss_url  = wss_url,
                tg_token = tg_tok,
                tg_chat  = tg_chat,
                sim      = sim,
            )
        except Exception as exc:
            self._set_status(f"Save failed: {exc}", "#ff4040")
            btn.label = original_label
            self._saving = False
            return

        if sim:
            self._set_status(
                "Saved — launching in SIMULATOR mode (no WSS configured).",
                "#00ff80",
            )
            mode_token = "sim"
        else:
            note = "  ·  https:// rewritten to wss://" if scheme_converted else ""
            self._set_status(f"Vault sealed. Launching radar…{note}", "#00ff80")
            mode_token = "live"

        # Hand control back to main.run_setup so the daemon can boot.
        self.exit(mode_token)

    # ── Helpers ────────────────────────────────────────────────────────────

    def _set_status(self, msg: str, color: str = "#8a7040") -> None:
        self.query_one("#status", Static).update(f"[{color}]{msg}[/{color}]")

    def _write_env(
        self,
        wss_url:  str,
        tg_token: str,
        tg_chat:  str,
        sim:      bool,
    ) -> None:
        """Encrypt sensitive fields and persist the .env file.

        Vault.encrypt is idempotent — re-running --setup with values
        that are already encrypted will not double-encrypt them, so
        prior input survives without manual cleanup.

        Telegram is enabled only when BOTH the token AND the chat ID
        are populated. A token without a chat target can't deliver; a
        chat ID without a token has no auth context.
        """
        from core.security import Vault

        wss_enc = Vault.encrypt(wss_url)  if wss_url  else ""
        tg_enc  = Vault.encrypt(tg_token) if tg_token else ""

        telegram_enabled = bool(tg_token and tg_chat)

        lines = [
            "# Whale Hunter v11.0 — generated by Sovereign Initialization",
            "# Sensitive values are encrypted with the local vault key.",
            "# Do NOT commit this file or the vault key to version control.",
            f"WHALE_SIMULATOR={'1' if sim else '0'}",
            f"WSS_URL={wss_enc}",
            f"TELEGRAM_BOT_TOKEN={tg_enc}",
            f"TELEGRAM_CHAT_ID={tg_chat}",
            f"TELEGRAM_ENABLED={'1' if telegram_enabled else '0'}",
            "WHALE_THRESHOLD_ETH=100.0",
            "INSIDER_THRESHOLD_USD=500000",
            "DORMANT_YEARS=5",
            "ALERT_SCORE_THRESHOLD=80",
            "ETH_PRICE_USD=3000",
            "MAX_QUEUE_SIZE=50000",
        ]
        self._env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
