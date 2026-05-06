"""
core/oracle.py — v6.0 Predictive Intelligence Engine.

OraclePredictor sits in the main process, downstream of the
mp.Queue→asyncio bridge. Every Signal that flows into the TUI also flows
through Oracle.observe(); on a pattern match it returns a synthesised
Signal of category=PREDICTIVE_ALERT that the dispatcher injects back
into the asyncio.Queue and Telegram alerter.

Two pattern detectors land in v6.0:

  • VIP Pre-Move (gas test):
      A wallet on the VIP watchlist sends a tiny outbound transaction
      (<= GAS_TEST_ETH_MAX). Empirically, this is how operators warm up
      a key / probe gas before a large move. Score 85.

  • Chain Reaction:
      Wallet B receives funds from Wallet A; within REACTION_WINDOW_SEC,
      Wallet B sends to a known bridge / DEX router. The pair flags as a
      coordinated funnel. Score 80.

Both detectors run in O(1) per signal. State is two bounded structures:

  _vip_last_seen     : OrderedDict[addr -> last_tx_time]    (LRU evict)
  _recent_recipients : OrderedDict[addr -> (ts, sender)]    (LRU evict)

No ML, no remote calls, no I/O — purely local rule-based detection. The
class is decoupled from the Daemon and the TUI; main.py wires it.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

from core.types import Signal, SignalCategory, HunterID, Transaction


@dataclass(slots=True)
class OracleConfig:
    """Tunable thresholds. Operators don't need to touch these in v6.0."""
    gas_test_eth_max:       float = 0.01     # tiny tx ceiling
    reaction_window_sec:    float = 30.0     # B->bridge must come within
    max_vip_history:        int   = 1_000    # LRU cap on _vip_last_seen
    max_recent_recipients:  int   = 5_000    # LRU cap on _recent_recipients
    pre_move_score:         int   = 85
    chain_reaction_score:   int   = 80


class OraclePredictor:
    """Stateful pattern detector that emits PREDICTIVE_ALERT signals."""

    def __init__(
        self,
        vip_wallets:      frozenset,
        bridge_addresses: frozenset = frozenset(),
        cfg:              Optional[OracleConfig] = None,
    ) -> None:
        self._vips     = vip_wallets or frozenset()
        self._bridges  = bridge_addresses or frozenset()
        self._cfg      = cfg or OracleConfig()
        self._vip_last_seen:     "OrderedDict[str, float]" = OrderedDict()
        self._recent_recipients: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
        # Telemetry counters — exposed via .stats
        self._stats: dict[str, int] = {
            "observed":         0,
            "pre_move_fires":   0,
            "chain_reactions":  0,
        }

    # ── Public API ──────────────────────────────────────────────────────

    def observe(self, signal: Signal) -> Optional[Signal]:
        """Process an incoming Signal. Returns an Oracle PREDICTIVE_ALERT
        Signal if a pattern is detected, else None.

        Side effects: updates _vip_last_seen and _recent_recipients with
        bounded growth (LRU eviction at maxlen). Pure on the inputs
        otherwise — safe to call from the asyncio loop hot path.
        """
        self._stats["observed"] += 1
        tx   = signal.transaction
        now  = time.monotonic()

        # ── Detector A: VIP Pre-Move (gas test) ─────────────────────────
        oracle = self._check_pre_move(tx, now)

        # ── Detector B: Chain Reaction (recipient -> bridge) ────────────
        if oracle is None:
            oracle = self._check_chain_reaction(tx, now)

        # State updates (always run, regardless of detection outcome)
        self._record_recipient(tx, now)
        self._record_vip(tx, now)

        return oracle

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    # ── Detectors ───────────────────────────────────────────────────────

    def _check_pre_move(self, tx: Transaction, now: float) -> Optional[Signal]:
        if not self._vips:
            return None
        sender = tx.from_addr.lower()
        if sender not in self._vips:
            return None
        if tx.value_eth > self._cfg.gas_test_eth_max:
            return None
        # VIP wallet just sent a sub-cent transfer. Classic "warm-up"
        # signature. Score is high but not maximum — the actual big move
        # is still ahead.
        self._stats["pre_move_fires"] += 1
        return Signal(
            category    = SignalCategory.PREDICTIVE_ALERT,
            hunter_id   = HunterID.ORACLE,
            score       = self._cfg.pre_move_score,
            transaction = tx,
            label       = "VIP PRE-MOVE",
            details     = (
                f"VIP {tx.from_addr[:14]}… sent {tx.value_eth:.6f} {tx.network} "
                f"(<= {self._cfg.gas_test_eth_max}) — gas-test pattern, "
                f"large move likely imminent."
            ),
            network     = tx.network,
            is_vip      = True,
        )

    def _check_chain_reaction(self, tx: Transaction, now: float) -> Optional[Signal]:
        if not self._bridges:
            return None
        if tx.to_addr.lower() not in self._bridges:
            return None
        sender = tx.from_addr.lower()
        prev = self._recent_recipients.get(sender)
        if prev is None:
            return None
        recv_ts, prev_from = prev
        if (now - recv_ts) > self._cfg.reaction_window_sec:
            # Stale — too long since the receive event.
            return None
        # B received from A within window AND is now sending to a known
        # bridge / DEX router. Coordinated funnel signature.
        self._stats["chain_reactions"] += 1
        return Signal(
            category    = SignalCategory.PREDICTIVE_ALERT,
            hunter_id   = HunterID.ORACLE,
            score       = self._cfg.chain_reaction_score,
            transaction = tx,
            label       = "CHAIN REACTION",
            details     = (
                f"{sender[:14]}… received from {prev_from[:14]}… "
                f"{(now - recv_ts):.1f}s ago, now routing to bridge "
                f"{tx.to_addr[:14]}… — funnel pattern."
            ),
            network     = tx.network,
        )

    # ── State helpers (bounded LRU) ─────────────────────────────────────

    def _record_recipient(self, tx: Transaction, now: float) -> None:
        recipient = tx.to_addr.lower()
        self._recent_recipients[recipient] = (now, tx.from_addr.lower())
        self._recent_recipients.move_to_end(recipient)
        # O(1) LRU evict
        while len(self._recent_recipients) > self._cfg.max_recent_recipients:
            self._recent_recipients.popitem(last=False)

    def _record_vip(self, tx: Transaction, now: float) -> None:
        sender = tx.from_addr.lower()
        if sender not in self._vips:
            return
        self._vip_last_seen[sender] = now
        self._vip_last_seen.move_to_end(sender)
        while len(self._vip_last_seen) > self._cfg.max_vip_history:
            self._vip_last_seen.popitem(last=False)
