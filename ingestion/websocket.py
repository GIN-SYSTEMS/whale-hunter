# Copyright (C) 2026 Whale Hunter Contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""
ingestion/websocket.py — Provider-agnostic WSS ingestion with reconnect.

V11.0 — Sovereign multi-provider support
----------------------------------------
The transport now subscribes via standard JSON-RPC. Two modes are supported,
auto-selected from the URL but overridable per-config:

  *  alchemy_pendingTransactions
     Default for *.alchemy.com URLs. The subscription event delivers the
     FULL transaction body in the notification payload, so parse_tx() can
     route immediately. This is the fastest path.

  *  newPendingTransactions
     Default for everything else (Infura, QuickNode, Ankr, Chainstack,
     Erigon, Geth, self-hosted). The subscription event delivers only the
     transaction HASH. We fan out an eth_getTransactionByHash RPC over the
     same socket and synthesise an Alchemy-shaped notification once the
     body comes back, so parse_tx() stays unchanged downstream.

A WSS_FALLBACK_URLS list is honoured: if the primary URL fails the auth
classifier, the chain worker rotates to the next entry on its next retry
instead of slamming the same dead endpoint forever.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import AsyncIterator, Optional
from urllib.parse import urlparse

import websockets

try:
    import orjson as json_lib
except ImportError:
    import json as json_lib

from core.config import Config

log = logging.getLogger("ingestion.wss")


# ── Provider auto-detection ──────────────────────────────────────────────────

def _detect_subscription_method(url: str) -> str:
    """Pick the right pending-tx subscription verb based on the WSS host.

    The Alchemy-extended `alchemy_pendingTransactions` is the fastest
    path because it ships the full tx body in the subscription event.
    Every other provider speaks plain JSON-RPC, where pending tx
    subscriptions deliver just the hash."""
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("alchemy.com") or host.endswith("alchemyapi.io"):
        return "alchemy_pendingTransactions"
    return "newPendingTransactions"


def _normalise_subscription_method(raw: str, url: str) -> str:
    """Honour an explicit operator override; otherwise auto-detect."""
    raw = (raw or "").strip().lower()
    if raw in ("auto", "", "default"):
        return _detect_subscription_method(url)
    if raw in ("alchemy", "alchemy_pendingtransactions", "alchemy_pendingTransactions".lower()):
        return "alchemy_pendingTransactions"
    if raw in ("standard", "geth", "newpendingtransactions"):
        return "newPendingTransactions"
    # Unknown → fall back to safe standard mode.
    return "newPendingTransactions"


# ── Error classification (unchanged) ─────────────────────────────────────────

def classify_wss_error(exc: BaseException) -> tuple[str, str]:
    """Classify a WebSocket connection failure into a user-actionable category.

    Returns (category, message). Categories:
      - NETWORK : firewall block, DNS failure, refused connection
      - TIMEOUT : handshake timed out (frequent on Eduroam / corporate proxies)
      - AUTH    : server rejected (bad API key, rate-limited, plan issue)
      - GENERIC : everything else
    """
    if isinstance(exc, asyncio.TimeoutError):
        return ("TIMEOUT",
                "WSS handshake timed out. Likely a NETWORK / FIREWALL "
                "RESTRICTION (common on Eduroam, corporate, or hotel networks "
                "that filter WebSocket ports). Try a VPN or a different network.")

    if isinstance(exc, ConnectionRefusedError):
        return ("NETWORK",
                "Connection refused: the endpoint is reachable but rejected "
                "the connection. Check the WSS URL and that the service is up.")

    if isinstance(exc, socket.gaierror):
        return ("NETWORK",
                "DNS resolution failed: cannot find the host. Check the WSS "
                "URL for typos and confirm your DNS is working.")

    if isinstance(exc, OSError):
        if exc.errno in (101, 113, 51, 50, 65, 10051, 10065):
            return ("NETWORK",
                    "Network unreachable. Check your connection or VPN.")

    try:
        from websockets.exceptions import (
            InvalidStatus, InvalidStatusCode, InvalidHandshake,
        )
        if isinstance(exc, (InvalidStatus, InvalidStatusCode)):
            code = (
                getattr(exc, "status_code", None)
                or getattr(getattr(exc, "response", None), "status_code", None)
            )
            if code in (401, 403):
                return ("AUTH",
                        f"Authentication failed (HTTP {code}). Your API key is "
                        f"invalid or expired. Re-run with --setup to update it.")
            if code == 429:
                return ("AUTH",
                        "Rate limit exceeded (HTTP 429). Slow down or upgrade "
                        "your provider plan.")
            if code is not None:
                return ("AUTH",
                        f"Server rejected the WSS handshake (HTTP {code}).")
        if isinstance(exc, InvalidHandshake):
            return ("NETWORK",
                    "WSS handshake malformed — likely a transparent HTTP proxy "
                    "(captive portal, corporate firewall) intercepting the "
                    "upgrade request.")
    except ImportError:
        pass

    return ("GENERIC", str(exc)[:240])


# ── Transport ────────────────────────────────────────────────────────────────

class RealWebSocketTransport:
    """
    Multichain WSS transport with auto-reconnect, provider-agnostic
    subscription, and primary/fallback URL rotation.

    Yields (network_id, raw_bytes) concurrently across chains.
    """

    # Cap fan-out: never let pending eth_getTransactionByHash requests
    # grow without bound under burst conditions.
    _MAX_PENDING_FETCHES = 4096

    def __init__(self, config: Config, queue: asyncio.Queue, stop_event=None, **kwargs):
        self._config     = config
        self._queue      = queue
        self._rate_limit = 5000
        self._stop       = stop_event

    def __aiter__(self):
        return self._merged_stream()

    # ── Per-chain ingestion loop ────────────────────────────────────────────

    async def _chain_worker(
        self,
        network: str,
        urls: list[str],
        merge_q: asyncio.Queue,
    ):
        """Drive one chain's WSS pipe. Rotates through `urls` on failure."""
        backoff   = 1.0
        url_idx   = 0
        sub_pref  = getattr(self._config, "wss_subscription_method", "auto")

        while True:
            if self._stop is not None and self._stop.is_set():
                return

            current_url = urls[url_idx % len(urls)]
            sub_method  = _normalise_subscription_method(sub_pref, current_url)
            sub_label   = "alchemy" if sub_method == "alchemy_pendingTransactions" else "standard"

            try:
                async with websockets.connect(
                    current_url,
                    max_size=2**22,
                    open_timeout=10,
                    close_timeout=5,
                ) as ws:
                    log.info(
                        "Connected to %s WSS (%s mode, endpoint=%s)",
                        network, sub_label, _safe_log_url(current_url),
                    )
                    backoff = 1.0  # reset on success

                    # ── Fire the subscription ────────────────────────────
                    if network == "SOL":
                        sub_payload = {
                            "jsonrpc": "2.0", "id": 1,
                            "method": "logsSubscribe", "params": ["all"],
                        }
                    else:
                        sub_payload = {
                            "jsonrpc": "2.0", "id": 1,
                            "method": "eth_subscribe", "params": [sub_method],
                        }
                    await _send_json(ws, sub_payload)

                    # ── Pump messages ────────────────────────────────────
                    if sub_method == "newPendingTransactions" and network != "SOL":
                        await self._pump_standard(ws, network, merge_q)
                    else:
                        await self._pump_alchemy(ws, network, merge_q)

            except asyncio.TimeoutError:
                log.warning("[%s] recv() timed out — reconnecting immediately", network)
                continue
            except Exception as e:
                if self._stop is not None and self._stop.is_set():
                    return
                category, message = classify_wss_error(e)
                if category in ("NETWORK", "TIMEOUT"):
                    log.error("[%s] NETWORK / FIREWALL — %s", network, message)
                elif category == "AUTH":
                    log.error("[%s] AUTH FAILURE — %s", network, message)
                else:
                    log.warning("[%s] WSS lost: %s", network, message)

                # Failover: try the next URL on the next attempt.
                if len(urls) > 1:
                    url_idx += 1
                    next_url = urls[url_idx % len(urls)]
                    log.info(
                        "[%s] Rotating to fallback endpoint %s (in %.1fs)",
                        network, _safe_log_url(next_url), backoff,
                    )

                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 60.0)

    # ── Pump: Alchemy-extended (subscription event carries full tx body) ──

    async def _pump_alchemy(self, ws, network: str, merge_q: asyncio.Queue):
        while True:
            if self._stop is not None and self._stop.is_set():
                return
            raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            try:
                merge_q.put_nowait((network, raw))
                self._queue.put_nowait(raw)
            except asyncio.QueueFull:
                pass  # back-pressure: drop oldest

    # ── Pump: Standard (subscription event carries hash, fetch body) ──────

    async def _pump_standard(self, ws, network: str, merge_q: asyncio.Queue):
        """Dual-track receiver: pending-hash subscription notifications fan
        out eth_getTransactionByHash requests; the responses are reshaped
        into Alchemy-format messages and fed to the same pipeline.

        We track outgoing fetch IDs in a set so non-fetch responses (other
        RPC traffic) don't get mistaken for tx bodies.
        """
        next_fetch_id = 1000
        pending_fetch_ids: set[int] = set()

        while True:
            if self._stop is not None and self._stop.is_set():
                return
            raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
            if isinstance(raw, bytes):
                raw_text = raw.decode("utf-8", errors="replace")
            else:
                raw_text = raw

            try:
                msg = json_lib.loads(raw_text)
            except Exception:
                continue

            # Case A: subscription notification → result is a hash string.
            if msg.get("method") == "eth_subscription":
                params = msg.get("params") or {}
                result = params.get("result")
                if isinstance(result, str) and result.startswith("0x"):
                    # Bound the fan-out: drop new hashes if we're already
                    # waiting on too many bodies. Prevents memory blow-up
                    # under mempool storms when a slow provider lags.
                    if len(pending_fetch_ids) >= self._MAX_PENDING_FETCHES:
                        continue
                    next_fetch_id += 1
                    pending_fetch_ids.add(next_fetch_id)
                    await _send_json(ws, {
                        "jsonrpc": "2.0",
                        "id": next_fetch_id,
                        "method": "eth_getTransactionByHash",
                        "params": [result],
                    })
                    continue

            # Case B: response to one of our fetches → reshape to Alchemy
            # format and emit downstream.
            mid = msg.get("id")
            if isinstance(mid, int) and mid in pending_fetch_ids:
                pending_fetch_ids.discard(mid)
                body = msg.get("result")
                if not isinstance(body, dict):
                    continue  # tx already mined / dropped — skip
                synthetic = json_lib.dumps({
                    "jsonrpc": "2.0",
                    "method":  "eth_subscription",
                    "params":  {"subscription": "synthetic", "result": body},
                })
                if isinstance(synthetic, str):
                    synthetic = synthetic.encode("utf-8")
                try:
                    merge_q.put_nowait((network, synthetic))
                    self._queue.put_nowait(synthetic)
                except asyncio.QueueFull:
                    pass

            # Anything else (subscription confirmations, errors, etc.) —
            # log and drop. We don't want them on the hot path.

    # ── URL list resolver ───────────────────────────────────────────────────

    def _urls_for(self, network: str) -> list[str]:
        """Build the [primary, *fallbacks] URL list for one chain."""
        primary = ""
        if network == "ETH":
            primary = self._config.wss_url
        elif network == "BASE":
            primary = getattr(self._config, "wss_base_url", "")
        elif network == "SOL":
            primary = getattr(self._config, "wss_sol_url", "")

        fallbacks = list(getattr(self._config, "wss_fallback_urls", []) or [])
        # Filter blanks, de-dupe while preserving order
        seen: set[str] = set()
        out:  list[str] = []
        for u in [primary, *fallbacks]:
            u = (u or "").strip()
            if not u or u in seen:
                continue
            seen.add(u)
            out.append(u)
        return out

    # ── Aggregator ──────────────────────────────────────────────────────────

    async def _merged_stream(self) -> AsyncIterator:
        merge_q: asyncio.Queue = asyncio.Queue(maxsize=2_000)

        tasks = []
        for network in ("ETH", "BASE", "SOL"):
            urls = self._urls_for(network)
            if urls:
                tasks.append(asyncio.create_task(
                    self._chain_worker(network, urls, merge_q),
                    name=f"wss-{network}",
                ))

        class Merged:
            async def __anext__(self):
                return await merge_q.get()
            def __aiter__(self): return self

        yield Merged()
        await asyncio.gather(*tasks, return_exceptions=True)


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _send_json(ws, payload: dict) -> None:
    """Encode + send a JSON message regardless of orjson/json availability."""
    msg = json_lib.dumps(payload)
    if isinstance(msg, str):
        msg = msg.encode("utf-8")
    await ws.send(msg)


def _safe_log_url(url: str) -> str:
    """Strip the API key path component before logging the endpoint."""
    try:
        parts = urlparse(url)
        if not parts.hostname:
            return "<invalid url>"
        # Don't leak the API key path component to logs.
        return f"{parts.scheme}://{parts.hostname}{':' + str(parts.port) if parts.port else ''}/<redacted>"
    except Exception:
        return "<unparseable>"
