import os
from typing import Optional, Callable
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.containers import Vertical, Horizontal
from textual.widgets import Label, Input, Button, DataTable, Static
from textual.binding import Binding
from core.config import Config


class TargetCommandCenter(ModalScreen):
    """V11.0 Target Command Center.

    Mutation hot-reload contract:
      - hot_reload_cb(config)  → daemon-side: re-arms the worker pool with
                                 the new targets dict so future signals
                                 are filtered against the updated set.
      - refresh_ui_cb()        → main-app side: re-renders the watchlist
                                 panel so the operator sees the new entry
                                 the instant they hit Enter — no waiting
                                 for the next signal, no modal close.
    Both callbacks fire after every successful add or delete.
    """

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Close"),
        Binding("t",      "app.pop_screen", "Close"),
    ]

    def __init__(
        self,
        config: Config,
        env_path: str,
        hot_reload_cb: Optional[Callable] = None,
        refresh_ui_cb: Optional[Callable] = None,
    ):
        super().__init__()
        self.config        = config
        self.env_path      = env_path
        self.hot_reload_cb = hot_reload_cb
        self.refresh_ui_cb = refresh_ui_cb

    def compose(self) -> ComposeResult:
        with Vertical(id="target-modal"):
            yield Label("WHALE HUNTER  ·  TARGET COMMAND CENTER", id="target-title")
            yield Label(
                "Paste an address. Type an alias (or leave blank for TGT-0x... auto-name). Enter to add.",
                id="target-subtitle",
            )
            yield DataTable(id="target-table", cursor_type="row")
            with Vertical(id="target-inputs"):
                yield Label("Address:")
                yield Input(
                    placeholder="0x... (EVM Address)",
                    id="address-input",
                )
                yield Label("Alias / Label (Optional):")
                yield Input(
                    placeholder="e.g., Binance Hot Wallet",
                    id="alias-input",
                )
            with Horizontal(id="target-buttons"):
                yield Button("Add Target",      variant="success", id="btn-add")
                yield Button("Delete Selected", variant="error",   id="btn-delete")
                yield Button("Close",           variant="primary",  id="btn-close")

    def on_mount(self) -> None:
        table = self.query_one("#target-table", DataTable)
        table.add_columns("Address", "Alias / Label")
        self.call_after_refresh(self._refresh_table)
        # Pre-focus address field so the operator can paste immediately.
        self.call_after_refresh(lambda: self.query_one("#address-input", Input).focus())

    # ── Keyboard-first: Enter in either Input triggers add ─────────────────────
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("address-input", "alias-input"):
            self._add_target()

    # ── Button dispatcher ──────────────────────────────────────────────────────
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-close":
            self.app.pop_screen()
        elif event.button.id == "btn-add":
            self._add_target()
        elif event.button.id == "btn-delete":
            self._delete_selected()

    # ── Core logic ─────────────────────────────────────────────────────────────
    def _add_target(self) -> None:
        input_addr  = self.query_one("#address-input", Input)
        input_label = self.query_one("#alias-input",   Input)

        address = input_addr.value.strip()

        # Address validation
        if not address.startswith("0x") or len(address) != 42:
            self.app.notify(
                "Invalid EVM address — must start with 0x and be 42 chars.",
                severity="error",
            )
            input_addr.focus()
            return

        # V11.0 — alias capture is bulletproof: read .value, strip, and
        # ONLY fall back to the TGT-0x... auto-name when the operator
        # genuinely left it blank. There is no "alias cannot be empty"
        # guard — empty is a valid input that triggers the auto-naming
        # branch. Whatever non-blank text the operator typed wins, full
        # stop. No silent overwriting, no validation rejection.
        manual_alias = input_label.value.strip()
        label = manual_alias if manual_alias else f"TGT-{address[:10]}"

        self.config.add_target(address, label, self.env_path)

        # QoL: clear fields, refocus address for the next paste
        input_addr.value  = ""
        input_label.value = ""
        input_addr.focus()

        self._refresh_table()
        self._fire_change_callbacks()

    def _delete_selected(self) -> None:
        table = self.query_one("#target-table", DataTable)
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            address = row_key.value
        except Exception:
            return  # nothing selected — DataTable cursor is the feedback

        self.config.remove_target(address, self.env_path)
        self._refresh_table()
        self._fire_change_callbacks()

    def _refresh_table(self) -> None:
        table = self.query_one("#target-table", DataTable)
        table.clear()
        for addr, label in self.config.targets.items():
            table.add_row(addr, label, key=addr)

    def _fire_change_callbacks(self) -> None:
        """Notify both the daemon (worker pool reload) and the parent App
        (watchlist panel re-render) that the target set has changed.

        The UI refresh is what makes the new alias appear instantly in
        the left tactical pane — without this hop the panel stays
        stale until the next inbound signal forces a re-render.

        V11.0 audit — pass our in-memory ``self.config.targets`` dict
        straight through to the UI refresh so it doesn't re-parse .env
        and re-decrypt the Vault just to read the watchlist we already
        have. Pure dict copy (microseconds) instead of disk + crypto."""
        if self.refresh_ui_cb:
            try:
                # Pass a snapshot — defensive copy so a later mutation
                # in self.config.targets doesn't race the renderer.
                self.refresh_ui_cb(dict(self.config.targets))
            except TypeError:
                # Older signature without the targets arg — fall back.
                try:
                    self.refresh_ui_cb()
                except Exception:
                    pass
            except Exception:
                # Never let a UI hiccup block the daemon hot-reload.
                pass
        if self.hot_reload_cb:
            try:
                self.hot_reload_cb(self.config)
            except Exception:
                pass