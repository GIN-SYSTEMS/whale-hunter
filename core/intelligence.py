"""
core/intelligence.py — Bayesian conviction scoring.

Applies per-hunter reliability weights and inter-hunter correlation bonuses
to produce a final conviction score for a transaction that triggered multiple
hunter engines simultaneously.

Formula:
    S_final = min(100, max(w_i × S_i  for all fired hunters)
                      + correlation_bonus(n_hunters))

Per-hunter weights encode domain confidence:
  DormantHunter    1.20  — near-certain when a genesis wallet moves
  CrimeWatch       1.15  — hack / OFAC addresses are definitive identifiers
  InsiderScanner   1.10  — nonce + value combo is a strong heuristic
  LiquiditySniper  1.05  — LP events from fresh wallets are unusual
  GovRadar         1.00  — baseline (public wallets but can be false-positive)
  StablecoinHunter 0.95  — large stablecoin moves are common, lower rarity
  FrontRunDetector 0.90  — gas spikes occur for non-MEV reasons too
  Correlator       1.00  — escalation output, not an independent source

Correlation bonus (added to weighted peak score):
  2 hunters fired  → +10 points
  3+ hunters fired → +20 points
"""

from __future__ import annotations

from core.types import HunterID, Signal

# ── Per-hunter reliability weights ───────────────────────
HUNTER_WEIGHTS: dict[HunterID, float] = {
    HunterID.DORMANT:    1.20,
    HunterID.CRIME:      1.15,
    HunterID.INSIDER:    1.10,
    HunterID.LIQUIDITY:  1.05,
    HunterID.GOV:        1.00,
    HunterID.CORRELATOR: 1.00,
    HunterID.STABLECOIN: 0.95,
    HunterID.FRONTRUN:   0.90,
}

_CORRELATION_BONUS: dict[int, int] = {0: 0, 1: 0, 2: 10, 3: 20}


def bayesian_final_score(signals: dict[HunterID, Signal]) -> int:
    """
    Compute Bayesian conviction score across all fired hunters.

    S_final = min(100, max(w_i × S_i) + correlation_bonus(n))
    """
    if not signals:
        return 0

    weighted_peak = max(
        HUNTER_WEIGHTS.get(hid, 1.0) * sig.score
        for hid, sig in signals.items()
    )
    n     = len(signals)
    bonus = _CORRELATION_BONUS.get(min(n, 3), 20)
    return min(100, int(weighted_peak + bonus))


def conviction_label(score: int) -> str:
    """Narrative label for a final conviction score."""
    if score >= 95:
        return "ABSOLUTE"
    if score >= 85:
        return "HIGH"
    if score >= 70:
        return "MODERATE"
    return "LOW"
