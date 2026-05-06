"""
alerts/telegram.py — Non-blocking Telegram Bot alerting.

Uses httpx for async HTTP. Only fires for high-confidence signals.
All alert formatting happens here — hunters stay pure.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Optional

import httpx

from core.types import Signal, SIGNAL_ART
from core.config import Config
from utils.explorer import explorer_url

log = logging.getLogger("whale_hunter.alerts.telegram")

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"
_COOLDOWN_SEC = 10.0  # Min seconds between alerts to avoid spam

# v5.0.1 Hotfix — strict 200 ETH threshold to prevent spam.
# The legacy score >= 80 trigger was removed. Alerts now ONLY fire for:
# 1. VIP targets
# 2. native >= 200 ETH AND score >= 80
_HIGH_VALUE_ETH_THRESHOLD = 200.0
_HIGH_VALUE_SCORE_THRESHOLD = 80

# Telegram bot tokens look like  "<bot_id>:<base64-ish secret>"
# (e.g.  123456:AAH...).  We use this to detect tokens that are still
# encrypted (`ENC:...`), blank, or otherwise malformed BEFORE building a
# request URL. A bad token is the difference between a clean radar and an
# endless 404 storm in the logs.
_TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{20,}$")


def _looks_like_valid_token(token: str) -> bool:
    if not token:
        return False
    # Anything still wearing the vault prefix can never be a valid token.
    if token.startswith("ENC:"):
        return False
    return bool(_TOKEN_RE.match(token))


class TelegramAlerter:
    """Async Telegram alert dispatcher. Non-blocking, fire-and-forget."""

    def __init__(self, config: Config):
        self._token = (config.telegram_bot_token or "").strip()
        self._chat_id = config.telegram_chat_id
        self._threshold = config.alert_score_threshold
        self._last_alert_time = 0.0
        self._client: Optional[httpx.AsyncClient] = None
        self._alert_count = 0
        self._poll_task: Optional[asyncio.Task] = None

        # Only enable if the operator opted in AND the token survives a
        # format check. A bad token used to silently spam 404s on every
        # alert + getUpdates tick.
        wants_telegram = config.telegram_enabled and bool(self._token)
        token_ok = _looks_like_valid_token(self._token)
        self._enabled = wants_telegram and token_ok

        if wants_telegram and not token_ok:
            log.warning(
                "Telegram disabled: bot token is missing, malformed, or still "
                "encrypted (vault decrypt failed). Re-run `python main.py --setup` "
                "to repair the credentials. No alerts will be dispatched this session."
            )
        # Opt-in command polling. Default OFF — most operators only need
        # outbound alerts and the polling loop was the loudest source of
        # 404s when the token was bad.
        self._commands_enabled = (
            self._enabled and os.getenv("TELEGRAM_COMMANDS", "0") == "1"
        )

    async def start(self) -> None:
        if not self._enabled:
            return
        self._client = httpx.AsyncClient(timeout=10.0)
        if self._commands_enabled:
            self._poll_task = asyncio.create_task(self._poll_commands())
            log.info("Telegram alerter initialized (with /commands polling).")
        else:
            log.info("Telegram alerter initialized (outbound only).")

    async def _poll_commands(self) -> None:
        """Background task to poll Telegram for /commands.

        Only runs when TELEGRAM_COMMANDS=1. On any 4xx (bad token, chat
        not found) we exit the loop instead of spinning — there is no
        recovery without operator intervention.
        """
        offset = 0
        backoff = 5.0
        while self._enabled and self._client:
            try:
                url = f"https://api.telegram.org/bot{self._token}/getUpdates"
                resp = await self._client.get(
                    url,
                    params={"offset": offset, "timeout": 30},
                    timeout=35.0,
                )
                if 400 <= resp.status_code < 500:
                    log.warning(
                        "Telegram getUpdates returned %d — disabling command "
                        "polling for this session.",
                        resp.status_code,
                    )
                    return
                if resp.status_code != 200:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
                    continue

                backoff = 5.0
                data = resp.json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    text = msg.get("text", "")
                    chat_id = msg.get("chat", {}).get("id")
                    if not text or not chat_id:
                        continue

                    if text.startswith("/filter "):
                        try:
                            self._threshold = int(text.split()[1])
                            await self._send_reply(
                                chat_id,
                                f"OK Threshold set to {self._threshold}",
                            )
                        except ValueError:
                            pass
                    elif text == "/status":
                        try:
                            import psutil
                            affinity = (
                                psutil.Process().cpu_affinity()
                                if hasattr(psutil.Process(), "cpu_affinity")
                                else "N/A"
                            )
                        except Exception:
                            affinity = "N/A"
                        status = (
                            f"<b>Whale Hunter v11.0 — SOVEREIGN Active</b>\n"
                            f"Threshold: {self._threshold}\n"
                            f"Alerts Sent: {self._alert_count}\n"
                            f"Core Affinity: {affinity}"
                        )
                        await self._send_reply(chat_id, status)
                    elif text == "/pause":
                        import main
                        main.PAUSED = True
                        await self._send_reply(chat_id, "Ingestion paused.")
                    elif text == "/resume":
                        import main
                        main.PAUSED = False
                        await self._send_reply(chat_id, "Ingestion resumed.")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("getUpdates poll error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                
    async def _send_reply(self, chat_id: int, text: str):
        if not (self._enabled and self._client):
            return
        url = _API_BASE.format(token=self._token)
        try:
            await self._client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
        except Exception:
            pass

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        if self._client:
            await self._client.aclose()
            self._client = None

    async def maybe_alert(self, signal: Signal) -> bool:
        """Decide whether a Signal warrants a Telegram alert and dispatch.

        v5.0.1 trigger matrix (any one is sufficient):
          (A) signal.is_vip                                 — bypass cooldown
          (B) value_eth >= 200 AND score >= 80              — high-value alpha

        VIP alerts skip the per-process cooldown so the operator never
        misses a watchlist hit.
        """
        if not self._enabled or not self._client:
            return False

        is_vip       = bool(getattr(signal, "is_vip", False))
        value_eth    = signal.transaction.value_eth
        is_hv_alpha  = (
            value_eth >= _HIGH_VALUE_ETH_THRESHOLD
            and signal.score >= _HIGH_VALUE_SCORE_THRESHOLD
        )

        if not (is_vip or is_hv_alpha):
            return False

        # Cooldown applies to non-VIP alerts only.
        if not is_vip:
            now = time.monotonic()
            if now - self._last_alert_time < _COOLDOWN_SEC:
                return False
            self._last_alert_time = now
        else:
            self._last_alert_time = time.monotonic()

        if is_vip:
            reason = "★ VIP TARGET"
        else:
            reason = f"HIGH-VALUE ALPHA (>={_HIGH_VALUE_ETH_THRESHOLD} ETH)"

        return await self.send_telegram_alert(signal, reason)

    async def send_telegram_alert(self, signal: Signal, reason: str) -> bool:
        """Direct tactical Telegram dispatcher. Non-blocking via httpx async.

        Used by maybe_alert() and any caller that wants to bypass the
        condition matrix and force-send an alert (e.g. system events
        like watchdog resurrection).
        """
        if not self._enabled or not self._client:
            return False

        self._alert_count += 1
        text = self._format_tactical(signal, reason)

        try:
            url = _API_BASE.format(token=self._token)
            resp = await self._client.post(url, json={
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            if resp.status_code == 200:
                log.info(
                    "Alert #%d sent: %s | score=%d | %.2f ETH",
                    self._alert_count, reason, signal.score,
                    signal.transaction.value_eth,
                )
                return True
            log.warning("Telegram API error: %d %s", resp.status_code, resp.text[:200])
            return False
        except Exception as exc:
            log.warning("Alert dispatch failed: %s", exc)
            return False

    async def send_system_message(self, text: str) -> bool:
        """Send a raw HTML system message directly to the configured chat."""
        if not self._enabled or not self._client or not self._chat_id:
            return False
        try:
            url = _API_BASE.format(token=self._token)
            resp = await self._client.post(url, json={
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
            })
            return resp.status_code == 200
        except Exception as exc:
            log.warning("System message failed: %s", exc)
            return False

    async def send_startup_message(self, chain: str) -> bool:
        """Send a silent test message on startup."""
        if not self._enabled or not self._client or not self._chat_id:
            return False
        text = f"WHALE HUNTER v11.0 — SOVEREIGN Online. Monitoring {chain}..."
        try:
            url = _API_BASE.format(token=self._token)
            resp = await self._client.post(url, json={
                "chat_id": self._chat_id,
                "text": text,
                "disable_notification": True,
            })
            return resp.status_code == 200
        except Exception as exc:
            log.warning("Startup message failed: %s", exc)
            return False

    @staticmethod
    def _format(signal: Signal) -> str:
        tx = signal.transaction
        return (
            f"<b>WHALE HUNTER ALERT #{signal.score}/100</b>\n\n"
            f"<b>{signal.label}</b>\n"
            f"Category: {signal.category.name}\n"
            f"Hunter: {signal.hunter_id.value}\n\n"
            f"Value: <b>{tx.value_eth:,.2f} ETH</b>\n"
            f"Gas: {tx.gas_price_gwei:,.1f} gwei\n"
            f"From: <code>{tx.from_addr[:16]}...</code>\n"
            f"To: <code>{tx.to_addr[:16]}...</code>\n"
            f"Hash: <code>{tx.tx_hash[:16]}...</code>\n\n"
            f"{signal.details}"
        )

    @staticmethod
    def _format_tactical(signal: Signal, reason: str) -> str:
        """V8.3 TARGET ALERT format. Structured for maximum readability on mobile."""
        tx    = signal.transaction
        chain = getattr(tx, "network", "ETH")
        alias = getattr(signal, "target_alias", None)
        url   = explorer_url(chain, tx.tx_hash, "tx")

        # Direction
        direction = "OUT ↓" if alias and "OUT" in signal.label else "IN ↑"

        # Value / Activity
        tt = getattr(signal, "token_transfer", None)
        if tt:
            amount_str = f"{tt.amount_human:,.0f} {tt.symbol}"
        elif tx.value_eth > 0:
            amount_str = f"{tx.value_eth:,.4f} {chain}"
        elif tx.input_data and len(tx.input_data) >= 10:
            sig_prefix = tx.input_data[:10].lower()
            amount_str = {
                "0xa9059cbb": "ERC-20 Transfer",
                "0x23b872dd": "ERC-20 TransferFrom",
                "0x095ea7b3": "Token Approve",
            }.get(sig_prefix, f"Contract Call ({sig_prefix})")
        else:
            amount_str = "0 ETH / Ping"

        target_line = f"<b>Alias:</b>    <code>{alias}</code>" if alias else \
                      f"<b>Address:</b>  <code>{tx.from_addr}</code>"

        return (
            f"🎯 <b>TARGET ALERT</b>\n"
            f"{'─' * 28}\n"
            f"{target_line}\n"
            f"<b>Action:</b>   {direction}\n"
            f"<b>Amount:</b>   {amount_str}\n"
            f"<b>Gas:</b>      {tx.gas_price_gwei:,.0f} gwei\n"
            f"<b>Tx:</b>       <a href=\"{url}\">{tx.tx_hash[:18]}…</a>\n"
            f"{'─' * 28}\n"
            f"<i>{signal.details[:80] if signal.details else ''}</i>"
        )


    @property
    def alert_count(self) -> int:
        return self._alert_count
