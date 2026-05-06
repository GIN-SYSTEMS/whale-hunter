"""
core/types.py — Shared data types for the Whale Hunter pipeline.

Pipeline flow: Raw JSON -> Transaction / TokenTransfer -> Signal -> Alert
"""


from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto


class SignalCategory(Enum):
    DORMANT_WAKING    = auto()
    INSTITUTIONAL     = auto()
    INSIDER_ALPHA     = auto()
    CRIME_FLOW        = auto()
    MEGA_WHALE        = auto()
    GAS_ANOMALY       = auto()
    VELOCITY_BURST    = auto()
    TOKEN_TRANSFER    = auto()   # ERC-20 stablecoin / high-cap whale move
    FRONT_RUN         = auto()   # Gas price velocity anomaly
    CRITICAL_ALPHA    = auto()   # Multi-hunter correlation escalation
    LIQUIDITY_MOVE    = auto()   # Uniswap v2/v3 Mint / Burn event
    PREDICTIVE_ALERT  = auto()   # v6.0 Oracle — pre-move / chain-reaction
    TARGET_HIT        = auto()   # v7.0 Watchlist Pivot


class HunterID(Enum):
    DORMANT     = "DormantHunter"
    GOV         = "GovRadar"
    INSIDER     = "InsiderScanner"
    CRIME       = "CrimeWatch"
    FRONTRUN    = "FrontRunDetector"
    STABLECOIN  = "StablecoinHunter"
    LIQUIDITY   = "LiquiditySniper"
    CORRELATOR  = "MultiHunterCorrelator"
    ORACLE      = "OraclePredictor"           # v6.0
    TARGETED    = "TargetedSentinel"          # v7.0


@dataclass(frozen=True, slots=True)
class Transaction:
    """Parsed Ethereum pending transaction."""
    tx_hash: str
    from_addr: str
    to_addr: str
    value_wei: int
    value_eth: float
    gas_price_gwei: float
    nonce: int
    input_data: str
    network: str = "ETH"
    timestamp: float = field(default_factory=time.time)

    def value_usd(self, eth_price: float = 3000.0) -> float:
        return self.value_eth * eth_price


@dataclass(frozen=True, slots=True)
class TokenTransfer:
    """Decoded ERC-20 transfer extracted from a pending transaction's input_data."""
    source_tx: Transaction   # Underlying ETH transaction (carries gas/nonce/from)
    contract: str            # Token contract address (lower-cased)
    symbol: str              # e.g. "USDT", "USDC", "DAI", "WBTC", "LINK"
    token_to: str            # Decoded recipient from input_data
    amount_raw: int          # Raw integer amount (before decimal shift)
    decimals: int            # Token decimal places
    amount_usd: float        # USD equivalent at time of detection

    @property
    def amount_human(self) -> float:
        """Human-readable token amount."""
        return self.amount_raw / (10 ** self.decimals)


@dataclass(slots=True)
class Signal:
    """Scored intelligence signal produced by a hunter engine."""
    category: SignalCategory
    hunter_id: HunterID
    score: int                # 0-100
    transaction: Transaction
    label: str
    details: str = ""
    network: str = "ETH"
    timestamp: float = field(default_factory=time.time)
    # v4.2 — operator's VIP watchlist hit. Set in core/daemon.py worker
    # when from_addr or to_addr is in Config.vip_wallets. Triggers a
    # red-row paint + terminal bell in the TUI and bypasses Telegram
    # alert cooldown.
    is_vip: bool = False
    token_transfer: Optional["TokenTransfer"] = None
    target_alias: Optional[str] = None

    @property
    def is_high_confidence(self) -> bool:
        return self.score > 80


# ── Wallet classification helpers ──────────────────────────

def wallet_tier(value_eth: float) -> str:
    """Classify transfer size into an operational tier."""
    if value_eth >= 100:
        return "WHALE"
    if value_eth >= 1:
        return "DOLPHIN"
    return "PRAWN"


def wallet_age_label(nonce: int) -> str:
    """Estimate wallet activity level from nonce (mempool proxy)."""
    if nonce == 0:
        return "GENESIS TX"
    if nonce <= 5:
        return f"NEW [{nonce} tx]"
    if nonce <= 50:
        return f"ACTIVE [{nonce} tx]"
    return f"VETERAN [{nonce}+ tx]"


# ── ASCII art blocks for tactical display ─────────────────

DORMANT_ART = r"""
  ╔══════════════════════════════════════╗
  ║   DORMANT WALLET WAKING             ║
  ║   Ancient address reactivated       ║
  ╚══════════════════════════════════════╝"""

INSTITUTIONAL_ART = r"""
  ╔══════════════════════════════════════╗
  ║   INSTITUTIONAL MOVE                ║
  ║   Government / Fund detected        ║
  ╚══════════════════════════════════════╝"""

INSIDER_ART = r"""
  ╔══════════════════════════════════════╗
  ║   INSIDER ALPHA DETECTED            ║
  ║   New wallet + massive value        ║
  ╚══════════════════════════════════════╝"""

CRIME_ART = r"""
  ╔══════════════════════════════════════╗
  ║   CRIME FLOW DETECTED              ║
  ║   Hack / Mixer interaction          ║
  ╚══════════════════════════════════════╝"""

WHALE_ART = r"""
  ╔══════════════════════════════════════╗
  ║   MEGA WHALE TRANSFER              ║
  ║   Massive value in motion           ║
  ╚══════════════════════════════════════╝"""

TOKEN_ART = r"""
  ╔══════════════════════════════════════╗
  ║   STABLECOIN / TOKEN WHALE          ║
  ║   Large ERC-20 transfer detected    ║
  ╚══════════════════════════════════════╝"""

FRONTRUN_ART = r"""
  ╔══════════════════════════════════════╗
  ║   FRONT-RUN DETECTED               ║
  ║   Gas velocity spike — MEV alert    ║
  ╚══════════════════════════════════════╝"""

CRITICAL_ART = r"""
  ╔══════════════════════════════════════╗
  ║ !! CRITICAL ALPHA — SCORE 100/100 !!║
  ║   Multi-hunter correlation fired    ║
  ║   DORMANT + CRIME | ESCALATED       ║
  ╚══════════════════════════════════════╝"""

LIQUIDITY_ART = r"""
  ╔══════════════════════════════════════╗
  ║   LIQUIDITY EVENT DETECTED          ║
  ║   Uniswap v2/v3 Mint / Burn         ║
  ╚══════════════════════════════════════╝"""

SIGNAL_ART = {
    SignalCategory.DORMANT_WAKING: DORMANT_ART,
    SignalCategory.INSTITUTIONAL:  INSTITUTIONAL_ART,
    SignalCategory.INSIDER_ALPHA:  INSIDER_ART,
    SignalCategory.CRIME_FLOW:     CRIME_ART,
    SignalCategory.MEGA_WHALE:     WHALE_ART,
    SignalCategory.TOKEN_TRANSFER: TOKEN_ART,
    SignalCategory.FRONT_RUN:      FRONTRUN_ART,
    SignalCategory.CRITICAL_ALPHA: CRITICAL_ART,
    SignalCategory.LIQUIDITY_MOVE: LIQUIDITY_ART,
}
