"""
core/daemon.py — v4.0 Pillar I: process isolation orchestrator.

Architecture
------------
    MAIN PROCESS  (this module is imported here)
        ├─ Textual TUI (asyncio)
        ├─ Watchdog (asyncio task)
        ├─ Telegram alerter (asyncio)
        └─ Bridge thread:  mp.Queue[Signal]  →  asyncio.Queue[Signal]

    INGESTOR  (one mp.Process)
        ├─ async WSS / simulator
        ├─ parse_tx → Transaction
        ├─ ClickHouseStorage (best-effort)
        └─ tx_queue.put((network, transaction))

    HUNTER WORKERS  (N mp.Process)
        ├─ tx_queue.get() → (network, transaction)
        ├─ run all hunters + correlator + stablecoin path
        └─ signal_queue.put(signal)

V11.0 Hot-Reload
----------------
The Daemon owns a multiprocessing.Manager().dict() that holds the live
target watchlist, plus an mp.Value('Q') version counter. Both are
passed to every ingestor and hunter worker at spawn time. Children
hold a frozen local snapshot keyed on the version they last saw; on
each tx they read the version int (atomic, no IPC), and only re-snapshot
the proxy when it has bumped. update_targets(new) clears+updates the
proxy and bumps the version atomically — every child picks up the
change on its next loop iteration without restarting. Zero downtime,
zero connection drop.
"""
from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp
import os
import pickle
import signal as signal_lib
import sys
import threading
import time
from queue import Empty, Full
from typing import Callable, Optional

from core.config import Config

log = logging.getLogger("whale_hunter.daemon")


# ── mp.Queue safe operations ──────────────────────────────────────────────────

def _safe_qsize(q: "mp.Queue") -> int:
    """qsize() raises NotImplementedError on macOS — degrade gracefully."""
    try:
        return q.qsize()
    except (NotImplementedError, OSError):
        return 0


def _safe_put_nowait(q: "mp.Queue", item) -> bool:
    try:
        q.put_nowait(item)
        return True
    except (Full, Exception):
        return False


async def _init_clickhouse_safe(clickhouse, ilog) -> None:
    """Run ClickHouse init under a tight timeout so a dead server cannot
    block the ingestion startup."""
    try:
        await asyncio.wait_for(clickhouse.start(), timeout=2.0)
    except asyncio.TimeoutError:
        ilog.warning("ClickHouse init timed out — running in memory-only mode")
    except Exception as exc:
        ilog.warning("ClickHouse init skipped: %s", exc)


# ── Worker entry points (MUST be module-level for spawn on Windows) ───────────

def _ingestor_worker(
    config_pickle: bytes,
    tx_queue: "mp.Queue",
    stop_event,
    tx_counter,
    chain_name: str = "ETH",
    wss_url_override: Optional[str] = None,
    raw_counter=None,      # mp.Value('Q') — counts every WSS message
    pulse_ts=None,         # mp.Value('d') — timestamp of last WSS message
    targets_proxy=None,    # mp.Manager().dict() — live target watchlist
    targets_version=None,  # mp.Value('Q') — bumped on every mutation
) -> None:
    """
    Long-running ingestor process. Owns the WSS connection (or simulator) and
    publishes (chain_name, Transaction) tuples to the tx_queue.

    V11.0 — the targets watchlist is read from a shared mp.Manager dict so
    UI mutations are visible to this loop within microseconds, without a
    process restart.
    """
    # Children must not respond to SIGINT — parent owns the shutdown protocol.
    signal_lib.signal(signal_lib.SIGINT, signal_lib.SIG_IGN)

    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [INGESTOR-{chain_name}] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    ilog = logging.getLogger(f"whale_hunter.ingestor.{chain_name.lower()}")
    ilog.info("PID %d starting (chain=%s)", os.getpid(), chain_name)

    config: Config = pickle.loads(config_pickle)
    if wss_url_override:
        try:
            object.__setattr__(config, "wss_url", wss_url_override)
        except Exception:
            pass

    if sys.platform == "linux":
        try:
            import uvloop
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        except ImportError:
            pass
    elif os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # ── Live targets snapshot ────────────────────────────────────────
    # Hold a frozen local set + the version counter we last saw.
    # On each tx we cheap-check the shared mp.Value; if it bumped we
    # snapshot the proxy in ONE IPC call, then filter against the
    # local set for the rest of the loop. Steady-state cost is a single
    # int read per tx — Manager round-trip only on actual mutation.
    if targets_proxy is not None:
        try:
            local_targets: set = set(targets_proxy.keys())
        except Exception:
            local_targets = set((config.targets or {}).keys())
    else:
        local_targets = set((config.targets or {}).keys())
    local_targets_version: int = (
        int(targets_version.value) if targets_version is not None else 0
    )

    def _maybe_refresh_targets() -> None:
        nonlocal local_targets, local_targets_version
        if targets_version is None or targets_proxy is None:
            return
        v = int(targets_version.value)
        if v == local_targets_version:
            return
        try:
            local_targets = set(targets_proxy.keys())
            local_targets_version = v
            ilog.info(
                "Targets hot-reload: %d entries (v=%d)",
                len(local_targets), v,
            )
        except Exception:
            # Manager may be tearing down — keep the last good snapshot.
            pass

    async def run() -> None:
        from main import parse_tx

        clickhouse = None
        if getattr(config, "use_clickhouse", False):
            import socket
            def is_ch_up():
                try:
                    with socket.create_connection(("localhost", 8123), timeout=0.1):
                        return True
                except OSError:
                    return False

            if is_ch_up():
                from ingestion.storage import ClickHouseStorage
                clickhouse = ClickHouseStorage()
                asyncio.create_task(_init_clickhouse_safe(clickhouse, ilog))
                ilog.info("ClickHouse enabled — async init in flight")
            else:
                ilog.warning("ClickHouse unreachable on localhost:8123 — skipping init, running in memory-only mode")
        else:
            ilog.info("ClickHouse disabled — in-memory mode (set USE_CLICKHOUSE=1 to enable)")

        if config.simulation_mode:
            from simulator import SimConfig, simulated_connect
            sim_config = SimConfig.from_env()
            transport = simulated_connect(sim_config, _NoOpQueue())
        else:
            from ingestion.websocket import RealWebSocketTransport
            transport = RealWebSocketTransport(config, _NoOpQueue())

        try:
            async for ws in transport:
                if stop_event.is_set():
                    break
                async for payload in ws:
                    if stop_event.is_set():
                        break
                    if isinstance(payload, tuple) and len(payload) == 2:
                        _net, raw = payload
                    else:
                        raw = payload
                    net = chain_name

                    try:
                        tx = parse_tx(raw, config.eth_price_usd, net)
                    except Exception as exc:
                        ilog.warning("Malformed WSS payload dropped: %s", exc)
                        continue

                    if raw_counter is not None:
                        raw_counter.value += 1
                    if pulse_ts is not None:
                        pulse_ts.value = time.time()

                    if tx is None:
                        continue

                    # V11.0 Hot-reload: pick up any UI-side mutations
                    # before applying the filter. Cheap int compare in
                    # the steady state, IPC snapshot only on bump.
                    _maybe_refresh_targets()

                    # V7.0 Drop the Ocean: Immediately discard non-targets
                    f_addr = tx.from_addr.lower()
                    t_addr = tx.to_addr.lower()
                    if f_addr not in local_targets and t_addr not in local_targets:
                        continue

                    tx_counter.value += 1

                    if clickhouse is not None:
                        try:
                            clickhouse.record(tx)
                        except Exception:
                            pass

                    if not _safe_put_nowait(tx_queue, (net, tx)):
                        pass
        except Exception as exc:
            ilog.error("Ingestor loop crashed: %s", exc, exc_info=True)
        finally:
            if clickhouse is not None:
                try:
                    await clickhouse.stop()
                except Exception:
                    pass

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    ilog.info("PID %d exiting", os.getpid())


class _LiveTargetsView:
    """Read-through proxy used by TargetedSentinel inside hunter workers.

    Wraps the shared mp.Manager().dict() so the hunter sees alias
    updates the moment the operator hits Enter in the UI — without a
    process restart. Caches a local snapshot keyed on the version
    counter so the hot path stays free of cross-process round-trips.
    """

    def __init__(self, proxy, version):
        self._proxy   = proxy
        self._version = version
        try:
            self._local: dict = dict(proxy) if proxy is not None else {}
        except Exception:
            self._local = {}
        self._v: int = int(version.value) if version is not None else 0

    def _maybe_refresh(self) -> None:
        if self._version is None or self._proxy is None:
            return
        v = int(self._version.value)
        if v == self._v:
            return
        try:
            self._local = dict(self._proxy)
            self._v = v
        except Exception:
            pass

    # Mapping protocol — TargetedSentinel.evaluate() only needs .get()
    # and __contains__, but we expose the rest for completeness in case
    # other call sites assume a dict-like.
    def get(self, key, default=None):
        self._maybe_refresh()
        return self._local.get(key, default)

    def __contains__(self, key) -> bool:
        self._maybe_refresh()
        return key in self._local

    def __len__(self) -> int:
        self._maybe_refresh()
        return len(self._local)

    def __iter__(self):
        self._maybe_refresh()
        return iter(self._local)

    def items(self):
        self._maybe_refresh()
        return self._local.items()

    def keys(self):
        self._maybe_refresh()
        return self._local.keys()

    def values(self):
        self._maybe_refresh()
        return self._local.values()


def _hunter_worker(
    config_pickle: bytes,
    tx_queue: "mp.Queue",
    signal_queue: "mp.Queue",
    stop_event,
    worker_id: int,
    eco_mode: bool,
    targets_proxy=None,    # mp.Manager().dict() — live target watchlist
    targets_version=None,  # mp.Value('Q') — bumped on every mutation
) -> None:
    """
    Hunter worker process. Pulls (network, Transaction) from tx_queue, runs
    all hunters + correlator, pushes Signal objects to signal_queue.

    V11.0 — TargetedSentinel reads its targets dict from a live view that
    refreshes on UI-side mutations without restarting this worker.
    """
    signal_lib.signal(signal_lib.SIGINT, signal_lib.SIG_IGN)

    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [WORKER-{worker_id}] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    wlog = logging.getLogger(f"whale_hunter.worker.{worker_id}")
    wlog.info("PID %d starting (eco=%s)", os.getpid(), eco_mode)

    config: Config = pickle.loads(config_pickle)

    from main import correlate
    from core.decoder import fast_decode_erc20
    from core.types import HunterID
    from hunters.targeted import TargetedSentinel

    hunter = TargetedSentinel(config)
    # Swap the static dict snapshot for a live view so alias additions
    # show up in evaluate() without restarting the worker.
    if targets_proxy is not None:
        hunter.targets = _LiveTargetsView(targets_proxy, targets_version)

    vips: frozenset = getattr(config, "vip_wallets", frozenset())

    def _tag_vip(sig):
        if not vips:
            return sig
        tx_in = sig.transaction
        if tx_in.from_addr.lower() in vips or tx_in.to_addr.lower() in vips:
            sig.is_vip = True
        return sig

    while not stop_event.is_set():
        try:
            item = tx_queue.get(timeout=0.5)
        except Empty:
            continue

        if item is None:  # sentinel
            break

        net, tx = item
        try:
            sig = hunter.evaluate(tx)
            if sig is not None:
                _safe_put_nowait(signal_queue, _tag_vip(sig))

            tt = fast_decode_erc20(tx)
            if tt is not None and sig is not None:
                sig.token_transfer = tt
        except Exception as e:
            wlog.error(f"Worker {worker_id} crashed on payload processing: {e}", exc_info=True)
            continue

    wlog.info("PID %d exiting", os.getpid())


class _NoOpQueue:
    """Compat stub for transports that expect an InstrumentedQueue but the
    ingestor no longer needs one (metrics are tracked via tx_counter)."""
    def put_nowait(self, _): pass
    def qsize(self): return 0
    @property
    def maxsize(self): return 0
    @property
    def fill_pct(self): return 0.0
    @property
    def metrics(self):
        class _M:
            total_dropped = 0
            total_enqueued = 0
            total_dequeued = 0
            peak_depth = 0
            drop_rate_pct = 0.0
            throughput_per_sec = 0.0
        return _M()


# ── Main-process bridge: mp.Queue[Signal]  →  asyncio.Queue[Signal] ──────────

def start_signal_bridge(
    mp_signal_queue: "mp.Queue",
    aio_signal_queue: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
    stop_event: threading.Event,
    on_signal: Optional[Callable] = None,
) -> threading.Thread:
    """
    Spawn a daemon thread that drains the mp Signal queue and schedules
    asyncio puts on the running loop. on_signal (sync callable) is invoked
    via call_soon_threadsafe per signal — used to dispatch Telegram /
    increment_alerts / monitor.tick from the main loop.
    """
    def _drain():
        while not stop_event.is_set():
            try:
                sig = mp_signal_queue.get(timeout=0.2)
            except Empty:
                continue
            if sig is None:
                break

            def _publish(s=sig):
                try:
                    aio_signal_queue.put_nowait(s)
                except asyncio.QueueFull:
                    pass
                if on_signal is not None:
                    try:
                        on_signal(s)
                    except Exception:
                        log.exception("on_signal callback failed")

            try:
                loop.call_soon_threadsafe(_publish)
            except RuntimeError:
                break

    t = threading.Thread(target=_drain, name="wh-signal-bridge", daemon=True)
    t.start()
    return t


# ── Daemon orchestrator ───────────────────────────────────────────────────────

class Daemon:
    """
    Spawns + supervises the ingestor and hunter worker processes, exposes
    the Signal mp.Queue + tx counter, and provides a graceful stop().
    """

    def __init__(
        self,
        config: Config,
        num_workers: Optional[int] = None,
        eco_mode: bool = False,
        tx_queue_size: int = 10_000,
        signal_queue_size: int = 10_000,
    ) -> None:
        if num_workers is None:
            num_workers = max(1, (os.cpu_count() or 4) - 2)
        self.config            = config
        self.num_workers       = num_workers
        self.eco_mode          = eco_mode
        self._tx_queue_size    = tx_queue_size
        self._signal_queue_size = signal_queue_size

        from core.config import ChainSpec
        if config.chains:
            self._chain_specs: list[ChainSpec] = list(config.chains)
        else:
            self._chain_specs = [
                ChainSpec.for_name(
                    getattr(config, "chain_type", "ETH") or "ETH",
                    getattr(config, "wss_url", "") or "",
                )
            ]

        self.tx_queue:     Optional[mp.Queue] = None
        self.signal_queue: Optional[mp.Queue] = None
        self.tx_counter                       = None
        self.raw_counter                      = None
        self.pulse_ts                         = None
        self.stop_event                       = None
        self.ingestors: list[mp.Process]      = []
        self.workers:   list[mp.Process]      = []
        self._started_at: float               = 0.0

        # V11.0 — live, mutable target watchlist shared across every
        # ingestor and worker. The Manager hosts a server process; child
        # procs hold proxies that round-trip on read. We pair it with a
        # version counter so children can cheaply skip the IPC trip
        # when nothing has changed (steady-state hot path).
        self._mgr                             = None
        self.targets_proxy                    = None
        self.targets_version                  = None

    @property
    def ingestor(self) -> Optional["mp.Process"]:
        """Back-compat single-ingestor accessor — returns the first."""
        return self.ingestors[0] if self.ingestors else None

    @property
    def chain_specs(self) -> list:
        return list(self._chain_specs)

    # ─ Lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> "mp.Queue":
        """Spawn one ingestor per chain + N hunter workers.
        Returns the shared mp signal queue."""
        ctx = mp.get_context("spawn")

        self.tx_queue     = ctx.Queue(maxsize=self._tx_queue_size)
        self.signal_queue = ctx.Queue(maxsize=self._signal_queue_size)

        # V11.0 audit — lock=False on every hot-path counter. The default
        # mp.Value wraps a kernel mutex (a Windows named semaphore on
        # the frozen .exe) and acquires it on EVERY .value read/write.
        # At 200 tx/s that's 200 * ~10 µs = 2 ms/s of pure lock overhead
        # in the ingestor loop, plus contention with the metrics readers
        # in the TUI. Going lock-free is correct here because:
        #   • tx_counter / raw_counter / pulse_ts are best-effort metrics;
        #     a lost increment under multi-chain race is invisible to the
        #     operator and acceptable.
        #   • 64-bit aligned writes are atomic on x86_64 (Windows + Linux),
        #     so torn reads are impossible — the value is either the old
        #     one or the new one, never garbage.
        #   • targets_version has a single writer (parent's update_targets);
        #     children only read. No race possible at all.
        self.tx_counter   = ctx.Value("Q", 0,   lock=False)  # target hits
        self.raw_counter  = ctx.Value("Q", 0,   lock=False)  # all WSS msgs
        self.pulse_ts     = ctx.Value("d", 0.0, lock=False)  # last WSS time
        self.stop_event   = ctx.Event()

        # V11.0 — boot the shared targets state BEFORE spawning children
        # so they receive live proxies, not None. Seeded from the
        # current Config so the first signal isn't filtered by an empty
        # set during the warmup window.
        self._mgr            = mp.Manager()
        seed                 = {k.lower(): v for k, v in (self.config.targets or {}).items()}
        self.targets_proxy   = self._mgr.dict(seed)
        # lock=False — single writer (parent only), 64-bit aligned atomic.
        self.targets_version = ctx.Value("Q", 1, lock=False)

        config_pickle = pickle.dumps(self.config)

        for spec in self._chain_specs:
            p = ctx.Process(
                target=_ingestor_worker,
                args=(
                    config_pickle, self.tx_queue, self.stop_event, self.tx_counter,
                    spec.name, spec.wss_url,
                    self.raw_counter, self.pulse_ts,
                    self.targets_proxy, self.targets_version,
                ),
                name=f"AG-Ingestor-{spec.name}",
                daemon=True,
            )
            p.start()
            self.ingestors.append(p)

        for i in range(self.num_workers):
            p = ctx.Process(
                target=_hunter_worker,
                args=(
                    config_pickle, self.tx_queue, self.signal_queue,
                    self.stop_event, i, self.eco_mode,
                    self.targets_proxy, self.targets_version,
                ),
                name=f"AG-Hunter-{i}",
                daemon=True,
            )
            p.start()
            self.workers.append(p)

        self._started_at = time.monotonic()
        log.info(
            "Daemon online: %d ingestor%s (%s) + %d hunters (PIDs %s)",
            len(self.ingestors), "" if len(self.ingestors) == 1 else "s",
            ", ".join(f"{s.name}@{p.pid}" for s, p in zip(self._chain_specs, self.ingestors)),
            self.num_workers,
            [w.pid for w in self.workers],
        )
        return self.signal_queue

    def stop(self, timeout: float = 6.0) -> None:
        """Graceful shutdown protocol: stop_event → sentinels → join → terminate."""
        if self.stop_event is None:
            return
        log.info("Daemon shutdown requested...")
        self.stop_event.set()

        for _ in range(self.num_workers + len(self.ingestors)):
            try:
                self.tx_queue.put_nowait(None)
            except Exception:
                break

        all_procs = list(self.ingestors) + self.workers
        deadline = time.monotonic() + timeout

        for p in all_procs:
            remaining = max(0.05, deadline - time.monotonic())
            p.join(timeout=remaining)
            if p.is_alive():
                log.warning("Process %s (PID %d) ignored sentinel — terminating", p.name, p.pid)
                p.terminate()
                p.join(timeout=1.0)
                if p.is_alive():
                    log.error("Process %s (PID %d) still alive — killing", p.name, p.pid)
                    p.kill()
                    p.join(timeout=1.0)

        for q in (self.tx_queue, self.signal_queue):
            if q is None:
                continue
            try:
                while True:
                    q.get_nowait()
            except Exception:
                pass
            try:
                q.close()
                q.join_thread()
            except Exception:
                pass

        # V11.0 — tear down the Manager last. Once children are gone
        # nothing else holds the proxy, so shutdown is safe.
        if self._mgr is not None:
            try:
                self._mgr.shutdown()
            except Exception:
                pass
            self._mgr = None
            self.targets_proxy = None
            self.targets_version = None

        log.info("Daemon stopped.")

    # ─ Hot-reload API ─────────────────────────────────────────────────────

    def update_targets(self, new_targets: dict) -> int:
        """V11.0 / V11.0 true hot-reload: push a new target watchlist to
        every ingestor and worker process WITHOUT restarting them.

        Returns
        -------
          new_version : int
            The bumped version counter on success — propagated back to
            the UI for visible "hot-reload v=N OK" confirmation.
          -1 on failure (e.g. called before daemon.start()).
        """
        if self.targets_proxy is None or self.targets_version is None:
            log.warning("update_targets called before daemon.start() — ignored")
            return -1

        normalized = {str(k).lower(): str(v) for k, v in (new_targets or {}).items()}

        try:
            self.targets_proxy.clear()
            if normalized:
                self.targets_proxy.update(normalized)
            # V11.0 — lock=False atomic write; sole writer is this method,
            # which itself runs on a single thread (the executor we
            # dispatched to from main.hot_reload_daemon). Concurrent
            # update_targets calls are serialised by the executor.
            self.targets_version.value += 1
        except Exception as exc:
            log.error("update_targets failed: %s", exc)
            return -1

        try:
            self.config.targets = normalized
        except Exception:
            pass

        new_version = int(self.targets_version.value)
        log.info(
            "Targets hot-updated: %d entries, version=%d (zero-restart)",
            len(normalized), new_version,
        )
        return new_version

    # ─ Supervisor utilities ───────────────────────────────────────────────

    def restart_ingestor(self) -> None:
        """Watchdog hook — respawns ALL ingestors (one per chain). Workers
        stay up because their state is worth preserving across a transient
        WSS hiccup. Multi-chain safe."""
        if not self.ingestors or self.stop_event is None:
            return
        log.warning("Watchdog: respawning %d ingestor%s",
                    len(self.ingestors), "" if len(self.ingestors) == 1 else "s")

        ctx = mp.get_context("spawn")
        config_pickle = pickle.dumps(self.config)
        new_ingestors: list[mp.Process] = []
        for spec, old in zip(self._chain_specs, self.ingestors):
            if old is not None and old.is_alive():
                old.terminate()
                old.join(timeout=2.0)
                if old.is_alive():
                    old.kill()
                    old.join(timeout=1.0)
            p = ctx.Process(
                target=_ingestor_worker,
                args=(
                    config_pickle, self.tx_queue, self.stop_event, self.tx_counter,
                    spec.name, spec.wss_url,
                    self.raw_counter, self.pulse_ts,
                    self.targets_proxy, self.targets_version,
                ),
                name=f"AG-Ingestor-{spec.name}",
                daemon=True,
            )
            p.start()
            new_ingestors.append(p)
        self.ingestors = new_ingestors

    def restart_all(self, new_config: Config) -> None:
        """Cold reload: respawn both ingestors and workers with a brand-new
        config. Use this only when something in Config OTHER than the
        target watchlist changed (e.g. WSS URL, Telegram credentials,
        threshold tuning). For target watchlist mutations call
        update_targets() instead — it's near-instant and keeps the WSS
        connection alive."""
        if not self.ingestors or self.stop_event is None:
            return
        log.warning("Daemon: cold-reloading config and respawning all processes")
        self.config = new_config

        if self.targets_proxy is not None and self.targets_version is not None:
            try:
                normalized = {
                    str(k).lower(): str(v)
                    for k, v in (new_config.targets or {}).items()
                }
                self.targets_proxy.clear()
                if normalized:
                    self.targets_proxy.update(normalized)
                # lock=False atomic write — see start() for rationale.
                self.targets_version.value += 1
            except Exception:
                pass

        for old in self.ingestors + self.workers:
            if old is not None and old.is_alive():
                old.terminate()
                old.join(timeout=1.0)
                if old.is_alive():
                    old.kill()

        ctx = mp.get_context("spawn")
        config_pickle = pickle.dumps(self.config)

        new_ingestors: list[mp.Process] = []
        for spec in self._chain_specs:
            p = ctx.Process(
                target=_ingestor_worker,
                args=(
                    config_pickle, self.tx_queue, self.stop_event, self.tx_counter,
                    spec.name, spec.wss_url,
                    self.raw_counter, self.pulse_ts,
                    self.targets_proxy, self.targets_version,
                ),
                name=f"AG-Ingestor-{spec.name}", daemon=True
            )
            p.start()
            new_ingestors.append(p)
        self.ingestors = new_ingestors

        new_workers: list[mp.Process] = []
        for i in range(self.num_workers):
            p = ctx.Process(
                target=_hunter_worker,
                args=(
                    config_pickle, self.tx_queue, self.signal_queue,
                    self.stop_event, i, self.eco_mode,
                    self.targets_proxy, self.targets_version,
                ),
                name=f"AG-Hunter-{i}", daemon=True
            )
            p.start()
            new_workers.append(p)
        self.workers = new_workers

    @property
    def tx_count(self) -> int:
        if self.tx_counter is None:
            return 0
        try:
            return int(self.tx_counter.value)
        except Exception:
            return 0

    @property
    def raw_count(self) -> int:
        if self.raw_counter is None:
            return 0
        try:
            return int(self.raw_counter.value)
        except Exception:
            return 0

    @property
    def last_pulse(self) -> float:
        """Unix timestamp of last WSS message received (0.0 = never)."""
        if self.pulse_ts is None:
            return 0.0
        try:
            return float(self.pulse_ts.value)
        except Exception:
            return 0.0

    @property
    def signal_queue_fill_pct(self) -> float:
        if self.signal_queue is None or self._signal_queue_size == 0:
            return 0.0
        return (_safe_qsize(self.signal_queue) / self._signal_queue_size) * 100.0

    def all_alive(self) -> bool:
        if not self.ingestors or not all(p.is_alive() for p in self.ingestors):
            return False
        return all(w.is_alive() for w in self.workers)

    def ingestor_states(self) -> list[dict]:
        """Per-chain ingestor state snapshot. Used by analytics view."""
        out: list[dict] = []
        for spec, proc in zip(self._chain_specs, self.ingestors):
            out.append({
                "chain": spec.name,
                "color": spec.color,
                "pid":   proc.pid if proc is not None else None,
                "alive": bool(proc and proc.is_alive()),
            })
        return out