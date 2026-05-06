# Copyright (C) 2026 Whale Hunter Contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""
main.py — Whale Hunter v11.0 — SOVEREIGN

Sovereign-Grade On-Chain Intelligence Radar.

Phases
------
  1. Gatekeeper   — First-run setup screen (auto-launched if .env is missing)
  2. Pipeline     — Async signal processing with global exception catcher
  3. TUI          — Brutalist Textual interface (Targets / Settings / Pause)

Modes
-----
  WHALE_SIMULATOR=1  →  Local simulation (no API keys needed)
  WHALE_SIMULATOR=0  →  Live WSS ingestion (any standard JSON-RPC provider)

Usage
-----
  python main.py             # Launch TUI mode (default)
  python main.py --headless  # Headless terminal mode (no Textual)
  python main.py --setup     # Force the initialization / config screen
  python main.py --no-db     # Force-disable ClickHouse persistence

Provider-Agnostic
-----------------
The transport accepts ANY standard JSON-RPC WSS endpoint — Alchemy, Infura,
QuickNode, Ankr, Chainstack, BlastAPI, GetBlock, LlamaNodes, PublicNode,
self-hosted Geth/Erigon/Nethermind/Reth. Paste either the HTTPS or the WSS
URL the provider gave you; the setup wizard auto-rewrites the scheme.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional


# ── Defensive console plumbing (fixes --noconsole / windowed PyInstaller) ────
#
# When PyInstaller compiles with --windowed (a.k.a. --noconsole) on Windows,
# the launcher detaches the process from a console and sets:
#     sys.stdout = sys.stderr = sys.stdin = None
# Any naïve `print()`, `sys.stdout.reconfigure(...)`, or `input()` call then
# explodes with:
#     AttributeError: 'NoneType' object has no attribute 'reconfigure'
#
# We patch the streams with a silent /dev/null-equivalent BEFORE any other
# code touches them. Real consoles (regular Python, --console PyInstaller,
# tmux/screen, SSH) are left untouched — only the None case is intercepted.
class _NullStream:
    """Silent stand-in for stdout/stderr/stdin under --noconsole builds.

    Implements just enough of the file-like protocol that print(),
    logging StreamHandler, and any third-party library that calls
    .reconfigure(), .write(), .flush(), .isatty(), or .readline()
    on the global streams will all behave as no-ops instead of raising.
    """
    encoding = "utf-8"
    errors   = "replace"
    closed   = False
    def write(self, _data):              return 0
    def writelines(self, _iterable):     return None
    def flush(self):                     return None
    def isatty(self):                    return False
    def reconfigure(self, **_kwargs):    return None
    def read(self, _n=-1):               return ""
    def readline(self, _n=-1):           return ""
    def close(self):                     self.closed = True
    def fileno(self):                    raise OSError("no fd in null stream")


if sys.stdout is None:
    sys.stdout = _NullStream()
if sys.stderr is None:
    sys.stderr = _NullStream()
if sys.stdin is None:
    sys.stdin = _NullStream()


# ── Windows UTF-8 console mode (post-_NullStream so it cannot crash) ─────────
if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        # GetStdHandle returns 0 (NULL) under --noconsole; SetConsoleMode
        # would then fail with ERROR_INVALID_HANDLE. Skip the call instead
        # of letting it bubble up as a generic Exception spam.
        if handle and handle != -1:
            kernel32.SetConsoleMode(handle, 7)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


try:
    import orjson as json_lib
except ImportError:
    import json as json_lib


from core.config import Config
from core.intelligence import bayesian_final_score, conviction_label
from core.telemetry import FrameRateMonitor, InstrumentedQueue, WalletHistoryCache
from core.types import (
    HunterID, Signal, SignalCategory, TokenTransfer, Transaction,
)
from alerts.telegram import TelegramAlerter
from ingestion.storage import ClickHouseStorage
from hunters import (
    CrimeWatch, DormantHunter, FrontRunDetector, GovRadar,
    InsiderScanner, LiquiditySniper, StablecoinHunter,
)
from utils.addresses import (
    ERC20_TRANSFER_SELECTOR, TOKEN_CONTRACT_ADDRS, TOKEN_CONTRACTS,
)


# ── Logging ───────────────────────────────────────────────────────────────────
# Keep the root channel on WARNING so third-party libraries (httpx, httpcore,
# websockets, asyncio, urllib3) cannot flood the terminal underneath the TUI.
# Our own loggers stay on INFO via the explicit setLevel below.
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
for _noisy in ("httpx", "httpcore", "websockets", "asyncio", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

log = logging.getLogger("whale_hunter")
log.setLevel(logging.INFO)


def _silence_console_for_tui() -> None:
    """Detach every StreamHandler from the root logger and route logs to a
    file instead. The Textual TUI owns the terminal — any stray INFO line
    written to stderr corrupts the layout. Called only in TUI mode."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            root.removeHandler(h)
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        try:
            file_handler = logging.FileHandler("whale_hunter.log", encoding="utf-8")
            file_handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            root.addHandler(file_handler)
        except OSError:
            # If the cwd is not writable just stay silent — the TUI is
            # more important than the log file.
            root.addHandler(logging.NullHandler())


# ── ANSI colours (headless mode) ──────────────────────────────────────────────
R   = "\033[0m"
RED = "\033[91m"
GRN = "\033[92m"
YLW = "\033[93m"
CYN = "\033[96m"
MAG = "\033[95m"
DIM = "\033[2m"
BLD = "\033[1m"
AMB = "\033[33m"


# ── Version + build metadata ──────────────────────────────────────────────────
WHALE_HUNTER_VERSION   = "11.0.0"
WHALE_HUNTER_CODENAME  = "SOVEREIGN"
WHALE_HUNTER_LICENSE   = "GPLv3"
WHALE_HUNTER_TAGLINE   = "Sovereign-Grade Mempool Sentinel"


def _render_banner() -> str:
    """Build the v11.0 SOVEREIGN boot banner.

    Designed to feel like a military-grade intelligence terminal coming
    online — classification stripe, codename, build/runtime metadata,
    and an ARMORY checklist that confirms each subsystem is wired.

    All ANSI escapes live on the BORDERS of the box; the interior text is
    pure ASCII so column alignment is preserved across every terminal,
    including older Windows consoles and bare ssh sessions.
    """
    import platform, socket
    py_ver = ".".join(str(x) for x in sys.version_info[:3])
    try:
        os_label = f"{platform.system()} {platform.release()}".strip()
    except Exception:
        os_label = "Unknown"
    try:
        hostname = socket.gethostname() or "unknown"
    except Exception:
        hostname = "unknown"
    boot_ts = time.strftime("%Y-%m-%d %H:%M:%S")

    rule = "═" * 79

    armory_lines = [
        ("SecurityVault",  "AES-128 / Fernet @ user config dir"),
        ("WSS Engine",     "Dual-Pump (Alchemy / Standard JSON-RPC)"),
        ("Hot-Reload",     "mp.Manager().dict() + atomic version flip"),
        ("Notification",   "Sovereign Shield (VIP-only, throttled 10s)"),
        ("Telegram",       "Encrypted bot token, non-blocking httpx"),
        ("TUI",            "Textual brutalist render @ 60+ FPS"),
    ]
    box_w = 70
    armory_box = []
    armory_box.append("  ┌" + "─" * (box_w - 2) + "┐")
    armory_box.append("  │  ARMORY".ljust(box_w + 1) + "│")
    armory_box.append("  ├" + "─" * (box_w - 2) + "┤")
    for label, desc in armory_lines:
        # Plain ASCII content — colour the [+] tick separately so the
        # padding math stays correct regardless of escape sequences.
        plain = f"  │  [+] {label:<14}  ·  {desc}"
        pad = max(0, (box_w + 2) - len(plain) - 1)
        armory_box.append(plain + " " * pad + "│")
    armory_box.append("  └" + "─" * (box_w - 2) + "┘")

    # Tint the [+] markers green — done after padding so visual width is
    # unaffected.
    armory_block = "\n".join(armory_box).replace("[+]", f"{GRN}[+]{R}")

    return (
        f"\n{CYN}{BLD}{rule}\n"
        f"██╗    ██╗██╗  ██╗ █████╗ ██╗     ███████╗  ██╗  ██╗██╗   ██╗███╗   ██╗████████╗███████╗██████╗\n"
        f"██║    ██║██║  ██║██╔══██╗██║     ██╔════╝  ██║  ██║██║   ██║████╗  ██║╚══██╔══╝██╔════╝██╔══██╗\n"
        f"██║ █╗ ██║███████║███████║██║     █████╗    ███████║██║   ██║██╔██╗ ██║   ██║   █████╗  ██████╔╝\n"
        f"██║███╗██║██╔══██║██╔══██║██║     ██╔══╝    ██╔══██║██║   ██║██║╚██╗██║   ██║   ██╔══╝  ██╔══██╗\n"
        f"╚███╔███╔╝██║  ██║██║  ██║███████╗███████╗  ██║  ██║╚██████╔╝██║ ╚████║   ██║   ███████╗██║  ██║\n"
        f" ╚══╝╚══╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚══════╝  ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝   ╚═╝   ╚══════╝╚═╝  ╚═╝\n"
        f"                                                              S O V E R E I G N  ·  v{WHALE_HUNTER_VERSION}\n"
        f"{rule}{R}\n"
        f"\n"
        f"  {AMB}CLASSIFICATION:{R}  UNCLASSIFIED // OPEN-SOURCE INTELLIGENCE\n"
        f"  {AMB}CODENAME:      {R}  WHALE HUNTER  ·  {WHALE_HUNTER_TAGLINE}\n"
        f"  {AMB}BUILD:         {R}  v{WHALE_HUNTER_VERSION} — {WHALE_HUNTER_CODENAME}  ·  {WHALE_HUNTER_LICENSE}\n"
        f"  {AMB}ARCHITECTURE:  {R}  No-AI Hot Path  ·  Multi-Process  ·  Provider-Agnostic\n"
        f"  {AMB}RUNTIME:       {R}  Python {py_ver}  ·  {os_label}  ·  {hostname}\n"
        f"  {AMB}TIMESTAMP:     {R}  {boot_ts}\n"
        f"\n"
        f"{DIM}{armory_block}{R}\n"
        f"\n"
        f"  {YLW}{BLD}>>>  STAND BY  ·  ENGAGING SENTINEL PROTOCOL  <<<{R}\n"
        f"{CYN}{rule}{R}\n"
    )


# Back-compat alias — kept so any external callers / tests that imported
# the old constant still work after the upgrade. _render_banner() is now
# the source of truth.
def _legacy_banner_proxy() -> str:
    return _render_banner()


BANNER = _render_banner()


# ── Env-path resolution ───────────────────────────────────────────────────────

def _is_installed() -> bool:
    """True when this module lives inside a Python site-packages directory
    (i.e. installed via pip), not the project tree."""
    import sysconfig
    here = Path(__file__).resolve().parent
    candidates = (
        sysconfig.get_paths().get("purelib"),
        sysconfig.get_paths().get("platlib"),
    )
    for raw in candidates:
        if not raw:
            continue
        site = Path(raw).resolve()
        if here == site or site in here.parents:
            return True
    return False


def _env_path() -> Path:
    """
    Return the .env path. Resolution order:
      1. $WHALE_HUNTER_CONFIG explicit override (or legacy $ANTIGRAVITY_CONFIG)
      2. Frozen executable: alongside the .exe
      3. Installed mode: $XDG_CONFIG_HOME/whale-hunter/.env
                         (fallback ~/.config/whale-hunter/.env)
                         Falls back to legacy ~/.config/antigravity/.env
                         if the new location is empty but the old one
                         exists, so existing setups keep working.
      4. Dev mode: alongside main.py
    """
    explicit = os.environ.get("WHALE_HUNTER_CONFIG") or os.environ.get("ANTIGRAVITY_CONFIG")
    if explicit:
        return Path(explicit).expanduser().resolve()

    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / ".env"

    if _is_installed():
        xdg_home = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg_home) if xdg_home else Path.home() / ".config"
        config_dir = base / "whale-hunter"
        config_dir.mkdir(parents=True, exist_ok=True)
        new_env = config_dir / ".env"
        if not new_env.exists():
            legacy_env = base / "antigravity" / ".env"
            if legacy_env.exists():
                return legacy_env
        return new_env

    return Path(__file__).resolve().parent / ".env"


# ── Transaction parsing ───────────────────────────────────────────────────────

def parse_tx(raw: bytes, eth_price: float, network: str = "ETH") -> Optional[Transaction]:
    """Parse a JSON-RPC subscription payload (Alchemy or standard) into a
    Transaction. Returns None for malformed / non-tx payloads."""
    try:
        data = json_lib.loads(raw)

        if network == "SOL":
            result = data.get("params", {}).get("result", {})
            val = result.get("value", {})
            sig = val.get("signature", "0x") if isinstance(val, dict) else "0x"
            err = val.get("err", None) if isinstance(val, dict) else None
            if err:
                return None
            return Transaction(
                tx_hash=sig,
                from_addr="SOL_NATIVE",
                to_addr="SOL_NATIVE",
                value_wei=0,
                value_eth=0.0,
                gas_price_gwei=0.0,
                nonce=0,
                input_data="",
                network="SOL",
            )

        result = data.get("params", {}).get("result")
        if not result:
            return None
        to_addr   = result.get("to") or "0x" + "0" * 40
        value_wei = int(result.get("value", "0x0"), 16)
        return Transaction(
            tx_hash        = result.get("hash", "0x"),
            from_addr      = result.get("from", "0x"),
            to_addr        = to_addr,
            value_wei      = value_wei,
            value_eth      = value_wei / 1e18,
            gas_price_gwei = int(result.get("gasPrice", "0x0"), 16) / 1e9,
            nonce          = int(result.get("nonce", "0x0"), 16),
            input_data     = result.get("input", "0x"),
            network        = network,
        )
    except Exception:
        return None


def parse_token_transfer(tx: Transaction) -> Optional[TokenTransfer]:
    """Decode an ERC-20 transfer call inside ``tx.input_data`` into a
    TokenTransfer with USD valuation. Returns None when the input is not a
    transfer call against a known token contract."""
    raw = tx.input_data
    if not raw or len(raw) < 138:
        return None
    if not raw.startswith(ERC20_TRANSFER_SELECTOR):
        return None
    contract = tx.to_addr.lower()
    if contract not in TOKEN_CONTRACT_ADDRS:
        return None
    try:
        body       = raw[10:]
        token_to   = "0x" + body[24:64]
        amount_raw = int(body[64:128], 16)
        meta       = TOKEN_CONTRACTS[contract]
        decimals   = meta["decimals"]
        amount_usd = (amount_raw / (10 ** decimals)) * meta["price_usd"]
        return TokenTransfer(
            source_tx  = tx,
            contract   = contract,
            symbol     = meta["symbol"],
            token_to   = token_to,
            amount_raw = amount_raw,
            decimals   = decimals,
            amount_usd = amount_usd,
        )
    except Exception:
        return None


# ── Multi-hunter correlation (Bayesian gate) ─────────────────────────────────

def correlate(signals: dict[HunterID, Signal], tx: Transaction) -> Optional[Signal]:
    """
    Bayesian correlation gate.

    Fires CRITICAL_ALPHA when:
      • DORMANT + CRIME on same tx  (highest conviction)
      • Any 3+ hunters triggered simultaneously
    Fires ALPHA_CORRELATION (LIQUIDITY_MOVE category, elevated) when:
      • LiquiditySniper + any of {DORMANT, CRIME, INSIDER}
    """
    n = len(signals)
    if n < 2:
        return None

    has_dormant   = HunterID.DORMANT   in signals
    has_crime     = HunterID.CRIME     in signals
    has_liquidity = HunterID.LIQUIDITY in signals
    has_insider   = HunterID.INSIDER   in signals

    is_critical = has_dormant and has_crime
    is_elevated = has_liquidity and (has_dormant or has_crime or has_insider)

    if not (is_critical or is_elevated or n >= 3):
        return None

    score      = bayesian_final_score(signals)
    conviction = conviction_label(score)
    fired      = " + ".join(hid.value for hid in signals)

    if is_critical or n >= 3:
        cat   = SignalCategory.CRITICAL_ALPHA
        label = "[!!] CRITICAL ALPHA"
    else:
        cat   = SignalCategory.LIQUIDITY_MOVE
        label = "[!] ALPHA CORRELATION"

    return Signal(
        category    = cat,
        hunter_id   = HunterID.CORRELATOR,
        score       = score,
        transaction = tx,
        label       = label,
        details     = (
            f"Bayesian: {score}/100 | Conviction: {conviction} | "
            f"Multi-hunter: {fired} | Hash: {tx.tx_hash[:18]}..."
        ),
    )


# ── Headless colour helper ────────────────────────────────────────────────────

def _signal_color(signal: Signal) -> str:
    return {
        SignalCategory.DORMANT_WAKING: YLW,
        SignalCategory.INSTITUTIONAL:  CYN,
        SignalCategory.INSIDER_ALPHA:  MAG,
        SignalCategory.CRIME_FLOW:     RED,
        SignalCategory.MEGA_WHALE:     GRN,
        SignalCategory.TOKEN_TRANSFER: AMB,
        SignalCategory.FRONT_RUN:      AMB,
        SignalCategory.CRITICAL_ALPHA: RED + BLD,
        SignalCategory.LIQUIDITY_MOVE: GRN,
    }.get(signal.category, R)


# ── Shared single-process pipeline (legacy fallback) ─────────────────────────

async def _run_pipeline(
    config:    Config,
    app_queue: InstrumentedQueue,
    monitor:   FrameRateMonitor,
    on_signal,
) -> None:
    """Single-process processing pipeline. Streams transactions, runs all
    hunters, emits signals. Raises on terminal WSS failure (caller handles
    retry). The v11.0 architecture uses ``Daemon`` instead — this stays as
    a no-fork fallback for environments where multiprocessing is broken."""
    from hunters.targeted import TargetedSentinel
    hunter = TargetedSentinel(config)

    executor   = concurrent.futures.ThreadPoolExecutor(max_workers=7)
    clickhouse = ClickHouseStorage()
    await clickhouse.start()

    if config.simulation_mode:
        from simulator import SimConfig, simulated_connect
        sim_config = SimConfig.from_env()
        transport  = simulated_connect(sim_config, app_queue)
    else:
        from ingestion.websocket import RealWebSocketTransport
        transport = RealWebSocketTransport(config, app_queue)

    global PAUSED
    PAUSED = False

    try:
        async for websocket in transport:
            async for payload in websocket:
                if PAUSED:
                    await asyncio.sleep(0.1)
                    continue

                if isinstance(payload, tuple) and len(payload) == 2:
                    network_id, raw = payload
                else:
                    network_id, raw = "ETH", payload

                tx = parse_tx(raw, config.eth_price_usd, network_id)
                if tx is None:
                    continue

                # V7.0 Drop the Ocean — filter early, emit nothing for
                # addresses outside the operator's watchlist.
                f_addr = tx.from_addr.lower()
                t_addr = tx.to_addr.lower()
                if f_addr not in config.targets and t_addr not in config.targets:
                    continue

                monitor.tick()

                sig = hunter.evaluate(tx)
                if sig:
                    from core.decoder import fast_decode_erc20
                    tt = fast_decode_erc20(tx)
                    if tt:
                        sig.token_transfer = tt
                    await on_signal(sig)

                clickhouse.record(tx)
    finally:
        await clickhouse.stop()


# ── TUI mode (multiprocess Daemon) ────────────────────────────────────────────

def _default_worker_count() -> int:
    """Pick a sane worker count for the hunter pool.

    The single ingestor is the WSS bottleneck, so anything beyond
    cpu_count - 1 buys nothing. Floor at 1 for tiny VPS boxes."""
    return max(1, (os.cpu_count() or 2) - 1)


async def run_tui(config: Config, env_path: Path) -> None:
    """v11.0 TUI runtime. Spawns the Daemon (per-chain ingestor + hunter pool)
    and bridges its mp.Queue[Signal] into an asyncio.Queue that the
    WhaleHunterApp consumes. The TUI process owns the alerter, the watchdog,
    the SIGINT shutdown protocol, and the Sovereign Notification Shield.
    """
    import threading
    from core.daemon import Daemon, start_signal_bridge

    from ui.interface import WhaleHunterApp

    # The Textual TUI owns the terminal — peel any console handlers off
    # the root logger and route everything to whale_hunter.log instead.
    _silence_console_for_tui()

    signal_queue: asyncio.Queue[Signal] = asyncio.Queue(maxsize=10_000)
    monitor      = FrameRateMonitor(window_sec=2.0)
    wallet_cache = WalletHistoryCache()
    alerter      = TelegramAlerter(config)
    await alerter.start()

    # V11.0 — Sovereign Notification Shield. Disabled by default; opt in
    # via OS_NOTIFICATIONS_ENABLED=1 in .env. Even when on, only fires
    # for VIP hits or transactions >= os_notify_min_eth, throttled to one
    # per os_notify_throttle_sec. The TUI is unaffected — full firehose.
    from core.notifications import NotificationShield
    notification_shield = NotificationShield(
        enabled      = getattr(config, "os_notifications_enabled", False),
        min_eth      = getattr(config, "os_notify_min_eth",        100.0),
        throttle_sec = getattr(config, "os_notify_throttle_sec",   10.0),
    )

    # exports/ resolution: next to the EXE (frozen), project root (dev),
    # or the user config dir (installed).
    if getattr(sys, "frozen", False):
        exports_dir = Path(sys.executable).parent / "exports"
    else:
        exports_dir = Path(__file__).resolve().parent / "exports"

    # ── Daemon spawn ──────────────────────────────────────────────────────
    daemon = Daemon(config, num_workers=_default_worker_count(), eco_mode=False)
    mp_signal_queue = daemon.start()

    # OraclePredictor lives in main process, downstream of the bridge. It
    # sees every Signal and emits PREDICTIVE_ALERT signals on pattern hit.
    # State is bounded LRU dicts (zero-leak).
    from core.oracle import OraclePredictor
    oracle = OraclePredictor(
        vip_wallets      = config.vip_wallets,
        bridge_addresses = getattr(config, "bridge_addresses", frozenset()),
    )

    def metrics_getter():
        # Extended contract for the Sovereign Terminal: analytics view
        # consumes ingestor_alive, workers_alive, worker_pids, tx_count.
        ingestor_alive = bool(daemon.ingestor and daemon.ingestor.is_alive())
        ingestor_pid   = daemon.ingestor.pid if daemon.ingestor else None
        worker_pids    = [w.pid for w in daemon.workers if w.pid]
        workers_alive  = sum(1 for w in daemon.workers if w.is_alive())
        return {
            "fill":           daemon.signal_queue_fill_pct,
            "dropped":        0,
            "tx_count":       daemon.tx_count,
            "raw_count":      daemon.raw_count,
            "last_pulse":     daemon.last_pulse,
            "ingestor_alive": ingestor_alive,
            "ingestor_pid":   ingestor_pid,
            "worker_count":   len(daemon.workers),
            "workers_alive":  workers_alive,
            "worker_pids":    worker_pids,
        }

    app_ready = asyncio.Event()

    def hot_reload_daemon(new_config: Config):
        """V11.0 — non-blocking hot-reload with visible TUI confirmation.

        Why this matters
        ----------------
        This callback is invoked from the Textual modal's button handler,
        which runs ON the asyncio event loop thread. The Manager proxy
        operations inside daemon.update_targets() are synchronous IPC
        calls — they pickle the mutation, send to the Manager subprocess
        via socket/pipe, wait for ack. On Windows this round-trip is
        5–50 ms. That's enough to drop a Textual frame and produce a
        visible UI stutter.

        Fix: dispatch to the default thread-pool executor. The UI loop
        returns to its render cycle in microseconds; the Manager IPC
        completes on a worker thread shortly after. Children still see
        the new targets within their next loop iteration because they
        watch a shared lock-free version counter, not the proxy itself.

        When the executor finishes, ``_on_done`` surfaces the result
        back to the TUI as a toast — operator gets immediate
        confirmation that the WSS loop now sees the new watchlist (or
        that the reload failed).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        target_count = len(new_config.targets or {})

        def _do_update() -> int:
            try:
                return daemon.update_targets(new_config.targets)
            except Exception as exc:
                log.error("Hot-reload Manager IPC failed: %s", exc)
                return -1

        def _on_done(fut):
            try:
                ver = fut.result()
            except Exception as exc:
                log.error("Hot-reload exec error: %s", exc)
                ver = -1
            try:
                if ver >= 0:
                    msg = f"Hot-reload OK · {target_count} target(s) · v={ver}"
                    severity = "information"
                else:
                    msg = "Hot-reload FAILED — check whale_hunter.log"
                    severity = "error"
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(
                        lambda: app.notify(msg, title="Watchlist", severity=severity, timeout=4.0)
                    )
            except Exception:
                pass

        if loop is not None and loop.is_running():
            # Returns immediately. UI stays at 60+ FPS.
            fut = loop.run_in_executor(None, _do_update)
            fut.add_done_callback(_on_done)
        else:
            # Defensive fallback: not in an async context, just run sync.
            _do_update()

        # Telegram side-channel — already non-blocking via httpx async,
        # we just need to schedule the coroutine onto the loop.
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(
                    lambda: loop.create_task(
                        alerter.send_system_message(
                            "🎯 <b>WATCHLIST UPDATED</b>: Targets hot-swapped (zero downtime)."
                        )
                    )
                )
            except Exception:
                pass

    app = WhaleHunterApp(
        signal_queue   = signal_queue,
        monitor         = monitor,
        wallet_cache    = wallet_cache,
        exports_dir     = exports_dir,
        metrics_getter  = metrics_getter,
        env_path        = env_path,
        chain_type      = getattr(config, "chain_type", "ETH"),
        app_ready       = app_ready,
        hot_reload      = hot_reload_daemon,
    )

    # ── mp.Queue[Signal] → asyncio.Queue[Signal] bridge ───────────────────
    # on_signal_sync runs on the asyncio loop (via call_soon_threadsafe).
    # It is the single dispatch point for monitor.tick + Telegram +
    # NotificationShield + alert counter.
    def on_signal_sync(sig: Signal) -> None:
        monitor.tick()

        # Oracle observes every signal in the main asyncio process. If a
        # pattern fires (VIP pre-move / chain-reaction) we get back a
        # synthesised PREDICTIVE_ALERT Signal that we inject into the
        # same asyncio.Queue the TUI consumes — it appears in the
        # DataTable like any other hunter output, plus an unconditional
        # Telegram blast (via send_telegram_alert directly, bypassing
        # cooldown).
        try:
            oracle_sig = oracle.observe(sig)
        except Exception:
            oracle_sig = None
        if oracle_sig is not None:
            try:
                signal_queue.put_nowait(oracle_sig)
            except asyncio.QueueFull:
                pass
            asyncio.create_task(
                alerter.send_telegram_alert(oracle_sig, reason="◆ ORACLE PREDICTIVE")
            )
            try:
                app.call_from_thread(app.increment_alerts)
            except Exception:
                pass

        # Strict alert gate — two independent triggers:
        #   • VIP target hit (operator-curated watchlist)
        #   • High-value alpha (>= 200 ETH AND score >= 80)
        # The alerter's maybe_alert() applies cooldown + reason logic.
        is_vip   = bool(getattr(sig, "is_vip", False))
        hv_alpha = (sig.transaction.value_eth >= 200.0 and sig.score >= 80)
        if is_vip or hv_alpha:
            async def _dispatch_alert():
                if await alerter.maybe_alert(sig):
                    try:
                        app.call_from_thread(app.increment_alerts)
                    except Exception:
                        pass
            asyncio.create_task(_dispatch_alert())

        # V11.0 — OS desktop notification shield. Independent of Telegram;
        # gated by its own VIP/value filter and throttled to one fire per
        # configured window. The TUI keeps streaming every signal silently
        # regardless of what the shield decides here.
        try:
            notification_shield.maybe_notify(sig)
        except Exception:
            pass

    bridge_stop = threading.Event()
    loop = asyncio.get_running_loop()

    async def _start_bridge_when_ready():
        await app_ready.wait()
        await alerter.send_startup_message(getattr(config, "chain_type", "ETH"))
        start_signal_bridge(
            mp_signal_queue, signal_queue, loop, bridge_stop,
            on_signal=on_signal_sync,
        )
        log.info("Signal bridge online.")

    bridge_starter = asyncio.create_task(_start_bridge_when_ready())

    # ── Watchdog — supervises ingestor (workers are stateless, ingestor
    # is the WSS connection holder). 5-minute silence on monitor.last_tick
    # → respawn ingestor in place; workers keep running.
    async def watchdog():
        await app_ready.wait()
        while True:
            await asyncio.sleep(60.0)
            if time.monotonic() - monitor.last_tick <= 300:
                continue

            log.error("Watchdog: signal stream silent >5m. Respawning ingestor...")
            try:
                daemon.restart_ingestor()
                monitor.last_tick = time.monotonic()
                asyncio.create_task(
                    alerter.send_system_message(
                        "🚨 <b>WATCHDOG TRIGGERED</b>: ingestor respawned after stall."
                    )
                )
                try:
                    app.call_from_thread(app.notify_watchdog_resurrection)
                except Exception:
                    pass
            except Exception as exc:
                log.error("Watchdog respawn failed: %s", exc)

    watchdog_task = asyncio.create_task(watchdog())

    try:
        await app.run_async()
    finally:
        # ── Graceful shutdown protocol ────────────────────────────────────
        log.info("TUI exit — initiating shutdown sequence")
        watchdog_task.cancel()
        bridge_starter.cancel()
        bridge_stop.set()
        try:
            daemon.stop(timeout=6.0)
        except Exception as exc:
            log.error("Daemon stop error: %s", exc)
        try:
            await alerter.stop()
        except Exception:
            pass


# ── Headless mode (multiprocess Ghost Core) ──────────────────────────────────

async def run_headless_clean(config: Config) -> None:
    """v11.0 headless. Same daemon topology as the TUI but with no Textual
    layer — signals are drained from the mp queue and logged."""
    import threading
    from queue import Empty
    from core.daemon import Daemon

    file_handler = logging.FileHandler("whale_hunter.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(file_handler)
    log.info("Headless mode activated.")
    print(f"{YLW}  Headless mode enabled. Check whale_hunter.log for alerts. UI disabled.{R}")

    monitor = FrameRateMonitor(window_sec=2.0)
    alerter = TelegramAlerter(config)
    await alerter.start()
    await alerter.send_startup_message(getattr(config, "chain_type", "ETH"))

    daemon = Daemon(config, num_workers=_default_worker_count(), eco_mode=False)
    mp_signal_queue = daemon.start()

    signal_count = 0
    drain_stop   = threading.Event()
    loop         = asyncio.get_running_loop()

    def _on_signal_main(sig: Signal) -> None:
        nonlocal signal_count
        signal_count += 1
        monitor.tick()
        tx = sig.transaction
        vip_tag = "★ " if getattr(sig, "is_vip", False) else ""
        log.info(
            f"{vip_tag}[{sig.label}] Score: {sig.score}/100 | "
            f"{tx.value_eth:,.2f} ETH | Hash: {tx.tx_hash}"
        )
        # Strict alert gate — VIP / high-value alpha.
        is_vip   = bool(getattr(sig, "is_vip", False))
        hv_alpha = (tx.value_eth >= 200.0 and sig.score >= 80)
        if is_vip or hv_alpha:
            asyncio.create_task(alerter.maybe_alert(sig))

    def _drain_loop():
        while not drain_stop.is_set():
            try:
                sig = mp_signal_queue.get(timeout=0.2)
            except Empty:
                continue
            if sig is None:
                break
            try:
                loop.call_soon_threadsafe(_on_signal_main, sig)
            except RuntimeError:
                break

    drain_thread = threading.Thread(target=_drain_loop, name="ghost-drain", daemon=True)
    drain_thread.start()

    async def telemetry_loop():
        while True:
            await asyncio.sleep(60.0)
            log.info(
                f"Telemetry: FPS={monitor.fps:,.0f} Avg={monitor.avg_fps:,.0f} "
                f"Signals={signal_count} TX={daemon.tx_count:,} "
                f"Workers_alive={daemon.all_alive()}"
            )

    async def headless_watchdog():
        while True:
            await asyncio.sleep(60.0)
            if time.monotonic() - monitor.last_tick <= 300:
                continue
            log.error("Watchdog: signal stream silent >5m. Respawning ingestor...")
            try:
                daemon.restart_ingestor()
                monitor.last_tick = time.monotonic()
            except Exception as exc:
                log.error("Watchdog respawn failed: %s", exc)

    t_start = time.perf_counter()
    telemetry_task = asyncio.create_task(telemetry_loop(), name="telemetry")
    watchdog_task  = asyncio.create_task(headless_watchdog(), name="watchdog")

    try:
        # Wait until shutdown signal (KeyboardInterrupt) reaches us.
        stop_future: asyncio.Future = asyncio.Future()
        await stop_future
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        log.info("Ghost Core shutdown — terminating daemon")
        drain_stop.set()
        telemetry_task.cancel()
        watchdog_task.cancel()
        try:
            daemon.stop(timeout=6.0)
        except Exception as exc:
            log.error("Daemon stop error: %s", exc)
        try:
            await alerter.stop()
        except Exception:
            pass

        elapsed = time.perf_counter() - t_start
        log.info(
            f"Session Summary: Duration={elapsed:,.1f}s "
            f"Avg_Throughput={monitor.avg_fps:,.0f}tx/s "
            f"Signals={signal_count} Alerts={alerter.alert_count}"
        )


# ── Gatekeeper ────────────────────────────────────────────────────────────────

def run_setup(env: Path) -> Optional[str]:
    """Run the Sovereign Initialization screen. Returns 'live', 'sim', or None."""
    from ui.setup import SetupApp
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    result = SetupApp(env_path=env).run()
    return result


def _needs_setup(env: Path) -> bool:
    """True when the .env is missing or lacks valid configuration."""
    if not env.exists():
        return True
    cfg    = Config.from_env(env)
    errors = cfg.validate()
    return bool(errors)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    # Render banner at runtime so the TIMESTAMP / RUNTIME fields show
    # the actual boot moment instead of the import moment.
    try:
        print(_render_banner())
    except Exception:
        # Never let a banner render error stop the boot.
        pass

    parser = argparse.ArgumentParser(
        description=f"Whale Hunter v{WHALE_HUNTER_VERSION} — {WHALE_HUNTER_CODENAME}: "
                    f"{WHALE_HUNTER_TAGLINE}"
    )
    parser.add_argument("--setup",    action="store_true", help="Force the initialization screen")
    parser.add_argument("--headless", action="store_true", help="Headless terminal mode (no Textual)")
    db_group = parser.add_mutually_exclusive_group()
    db_group.add_argument("--db",    dest="db_flag", action="store_true", default=None,
                          help="Enable ClickHouse persistence (overrides USE_CLICKHOUSE)")
    db_group.add_argument("--no-db", dest="db_flag", action="store_false",
                          help="Skip ClickHouse entirely — fastest boot, in-memory only")
    args = parser.parse_args()

    env = _env_path()
    force_setup = args.setup

    # ── Phase 1: Gatekeeper ───────────────────────────────────────────────────
    if force_setup or _needs_setup(env):
        result = run_setup(env)
        if result is None:
            print(f"{YLW}  Initialization aborted.{R}")
            sys.exit(0)
        print(f"{GRN}  Configuration saved. Mode: {result.upper()}{R}\n")

    # ── Load config ───────────────────────────────────────────────────────────
    config = Config.from_env(env)

    # CLI flag overrides env-var USE_CLICKHOUSE
    if args.db_flag is not None:
        config.use_clickhouse = args.db_flag

    errors = config.validate()
    if errors:
        print(f"{RED}  Configuration errors:{R}")
        for e in errors:
            print(f"  {RED}  - {e}{R}")
        sys.exit(1)

    mode_str = "SIMULATION" if config.simulation_mode else "LIVE"
    print(f"{GRN}  Config validated. Source: {mode_str}{R}")

    headless = args.headless

    if sys.platform == "linux":
        try:
            import uvloop
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
            log.info("uvloop event policy engaged for extreme low-latency.")
        except ImportError:
            log.warning("uvloop not installed, falling back to asyncio.")
    elif os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # ── Phase 2 + 3: Pipeline + TUI with global exception catcher ─────────────
    try:
        if headless:
            asyncio.run(run_headless_clean(config))
        else:
            try:
                asyncio.run(run_tui(config, env))
            except ImportError as exc:
                print(f"{YLW}  Textual import error ({exc}). Falling back to headless.{R}\n")
                asyncio.run(run_headless_clean(config))
    except KeyboardInterrupt:
        print(f"\n{YLW}  Shutdown by user.{R}")
    except Exception as exc:
        # Global exception catcher — prevents EXE from vanishing on crash.
        log.exception("Unhandled exception in main loop")
        print(f"\n{RED}{'='*62}")
        print(f"  FATAL ERROR: {type(exc).__name__}")
        print(f"  {exc}")
        print(f"{'='*62}{R}")
        print(f"\n  Check the log above for the full traceback.")
        print(f"  Re-launch with WHALE_SIMULATOR=1 for offline debugging.\n")
        if os.name == "nt":
            # Keep the console window open so the operator can read the
            # error. Skip silently under --noconsole / windowed builds
            # where stdin is a _NullStream — input() would just return
            # immediately and EOF the loop.
            if not isinstance(sys.stdin, _NullStream):
                try:
                    input("  Press Enter to exit...")
                except (EOFError, OSError):
                    pass
        sys.exit(1)


if __name__ == "__main__":
    # MANDATORY for PyInstaller-frozen builds on Windows.
    #
    # Without this call, every mp.Process child spawned via the "spawn"
    # context re-imports the frozen __main__ and re-runs main(), which
    # re-spawns its own children — an unbounded fork-bomb that flashes
    # the banner, opens a Manager subprocess, and then crashes the
    # parent inside seconds. freeze_support() short-circuits the child
    # entry point so it loads its assigned worker function instead.
    #
    # Cheap no-op when running from source / interpreter.
    import multiprocessing as _mp
    _mp.freeze_support()
    main()
