"""
core/config.py — Configuration management.

Loads from .env file with a zero-dependency parser.
Validates credentials before launch.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── v6.0 Omni-Chain — ChainSpec ────────────────────────────
#
# A chain is a (name, wss_url, color) triple. The Daemon spawns one
# ingestor process per ChainSpec; each pushes (chain_name, Transaction)
# into the shared mp tx_queue, where round-robin workers run hunters
# regardless of chain. The UI uses .color to paint the Chain column.

# rich-text colour names per chain — keep in sync with utils/explorer.py
# explorer bases. Unknown chains default to "white".
_CHAIN_COLORS: dict[str, str] = {
    "ETH":  "bright_blue",
    "BASE": "blue",
    "ARB":  "cyan",
    "POLY": "magenta",
    "BSC":  "yellow",
    "OPT":  "red",
    "SOL":  "bright_magenta",
    "BTC":  "orange1",
}


@dataclass(frozen=True, slots=True)
class ChainSpec:
    """A single blockchain ingestion target. Picklable across mp processes."""
    name:    str            # canonical short name: "ETH" / "BASE" / etc.
    wss_url: str            # WebSocket endpoint for live mempool
    color:   str = "white"  # rich-text colour for the UI Chain column

    @classmethod
    def for_name(cls, name: str, wss_url: str) -> "ChainSpec":
        """Build a ChainSpec with the canonical colour for the name."""
        n = name.upper()
        return cls(name=n, wss_url=wss_url, color=_CHAIN_COLORS.get(n, "white"))


@dataclass
class Config:
    """Master configuration for Whale Hunter: Sovereign-Grade On-Chain Intelligence Radar."""

    # WSS Endpoints
    wss_url: str = ""
    wss_base_url: str = ""
    wss_sol_url: str = ""
    wss_backup_url: str = ""

    # V11.0 — provider-agnostic ingestion controls.
    #   wss_subscription_method:
    #     "auto"     → detect from URL (alchemy.com → alchemy_pendingTransactions,
    #                  everything else → newPendingTransactions). Default.
    #     "alchemy"  → force alchemy_pendingTransactions (full body in event)
    #     "standard" → force newPendingTransactions (hash + eth_getTransactionByHash
    #                  follow-up over the same socket; works on any JSON-RPC
    #                  provider — Infura, QuickNode, Ankr, Chainstack, Geth,
    #                  Erigon, self-hosted nodes).
    #   wss_fallback_urls:
    #     CSV-loaded list of backup endpoints. The transport rotates to the
    #     next entry every time the primary fails (auth/network/timeout).
    wss_subscription_method: str = "auto"
    wss_fallback_urls: list = field(default_factory=list)

    # V11.0 — OS desktop notification shield. Disabled by default; the TUI
    # already shows the firehose silently. Enable for VIP-only escalation
    # to the operating system tray (Windows toast / macOS Notification
    # Center / Linux libnotify), throttled and value-filtered so the OS
    # never spams.
    os_notifications_enabled: bool = False
    os_notify_min_eth: float = 100.0       # only fire when tx value >= this
    os_notify_throttle_sec: float = 10.0   # min seconds between fires

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_enabled: bool = False

    # Thresholds
    whale_threshold_eth: float = 100.0
    insider_threshold_usd: float = 500_000.0
    dormant_years: int = 5
    alert_score_threshold: int = 80

    # Performance
    max_queue_size: int = 50_000
    noise_display_interval: int = 500
    reconnect_max_retries: int = 100
    reconnect_base_delay: float = 1.0
    reconnect_max_delay: float = 60.0

    # Market
    eth_price_usd: float = 3_000.0

    # Mode
    simulation_mode: bool = False

    # Storage
    # ClickHouse off by default — most operators don't run a local CH server.
    # Enable explicitly via USE_CLICKHOUSE=1 in .env or omit --no-db on CLI.
    use_clickhouse: bool = False

    # Active blockchain. Used by the Header sub_title + the InspectionModal
    # external-explorer launcher to pick the correct block-explorer base
    # URL. Recognised values: ETH | BASE | ARB | POLY | BSC | OPT | SOL |
    # BTC | MULTI (label only — the actual ingestion runs against
    # whatever WSS_URL points at).
    chain_type: str = "ETH"

    # v7.0 Watchlist Pivot
    targets: dict = field(default_factory=dict)
    
    # VIP target watchlist (v4.2). Legacy support.
    vip_wallets: frozenset = field(default_factory=frozenset)

    # v6.0 Omni-Chain — list of ChainSpec to ingest in parallel. Empty
    # means "use single-chain back-compat" (chain_type + wss_url above).
    # Populated from CHAIN_<NAME>_URL env vars (e.g. CHAIN_ETH_URL,
    # CHAIN_BASE_URL, CHAIN_BSC_URL) — see _load_chains() below.
    chains: list = field(default_factory=list)

    # v6.0 Bridge / DEX router watchlist for OraclePredictor's
    # chain-reaction detector. Lower-cased addresses. Operators add via
    # BRIDGE_ADDRESSES (CSV in .env) — defaults to a small starter set.
    bridge_addresses: frozenset = field(default_factory=frozenset)

    @classmethod
    def from_env(cls, env_path: Optional[Path] = None) -> Config:
        if env_path is None:
            if getattr(sys, "frozen", False):
                # PyInstaller EXE: look for .env next to the executable
                base_dir = Path(sys.executable).parent
                env_path = base_dir / ".env"
                # No .env found → fall back to simulator mode (safe default)
                if not env_path.exists() and "WHALE_SIMULATOR" not in os.environ:
                    os.environ["WHALE_SIMULATOR"] = "1"
            else:
                env_path = Path(__file__).resolve().parent.parent / ".env"
        if env_path.exists():
            _load_dotenv(env_path)

        from core.security import Vault

        return cls(
            wss_url=Vault.decrypt(os.getenv("WSS_URL", "")),
            wss_base_url=Vault.decrypt(os.getenv("WSS_BASE_URL", "")),
            wss_sol_url=Vault.decrypt(os.getenv("WSS_SOL_URL", "")),
            wss_backup_url=Vault.decrypt(os.getenv("WSS_BACKUP_URL", "")),
            wss_subscription_method=os.getenv("WSS_SUBSCRIPTION_METHOD", "auto"),
            wss_fallback_urls=_load_wss_fallback_urls(),
            os_notifications_enabled=os.getenv("OS_NOTIFICATIONS_ENABLED", "0") == "1",
            os_notify_min_eth=float(os.getenv("OS_NOTIFY_MIN_ETH", "100")),
            os_notify_throttle_sec=float(os.getenv("OS_NOTIFY_THROTTLE_SEC", "10")),
            telegram_bot_token=Vault.decrypt(os.getenv("TELEGRAM_BOT_TOKEN", "")),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            telegram_enabled=os.getenv("TELEGRAM_ENABLED", "0") == "1",
            whale_threshold_eth=float(os.getenv("WHALE_THRESHOLD_ETH", "100")),
            insider_threshold_usd=float(os.getenv("INSIDER_THRESHOLD_USD", "500000")),
            dormant_years=int(os.getenv("DORMANT_YEARS", "5")),
            alert_score_threshold=int(os.getenv("ALERT_SCORE_THRESHOLD", "80")),
            eth_price_usd=float(os.getenv("ETH_PRICE_USD", "3000")),
            simulation_mode=os.getenv("WHALE_SIMULATOR", "0") == "1",
            use_clickhouse=os.getenv("USE_CLICKHOUSE", "0") == "1",
            chain_type=os.getenv("CHAIN_TYPE", "ETH").upper(),
            targets=_load_targets(env_path),
            vip_wallets=_load_vip_wallets(env_path),
            chains=_load_chains(),
            bridge_addresses=_load_bridge_addresses(),
        )

    def validate(self) -> list[str]:
        errors = []
        if not self.simulation_mode and not self.wss_url:
            errors.append("WSS_URL required for live mode. Set WHALE_SIMULATOR=1 for sim.")
        if self.telegram_enabled:
            if not self.telegram_bot_token:
                errors.append("TELEGRAM_BOT_TOKEN required when TELEGRAM_ENABLED=1")
            if not self.telegram_chat_id:
                errors.append("TELEGRAM_CHAT_ID required when TELEGRAM_ENABLED=1")
        return errors

    def add_target(self, address: str, label: str, env_path: Optional[Path] = None) -> None:
        addr_lower = address.lower()
        self.targets[addr_lower] = label
        self._flush_targets(env_path)

    def remove_target(self, address: str, env_path: Optional[Path] = None) -> None:
        addr_lower = address.lower()
        if addr_lower in self.targets:
            del self.targets[addr_lower]
            self._flush_targets(env_path)

    def _flush_targets(self, env_path: Optional[Path]) -> None:
        import json
        targets_file = Path(env_path).resolve().parent / "targets.json" if env_path else Path("targets.json")
        out_data = [{"address": k, "alias": v} for k, v in self.targets.items()]
        try:
            targets_file.write_text(json.dumps(out_data, indent=2), encoding="utf-8")
        except Exception:
            pass


_CHAIN_ENV_PATTERN = re.compile(r"^CHAIN_([A-Z0-9]+)_URL$")

# Conservative bridge / DEX router starter set. Operators extend via
# BRIDGE_ADDRESSES env CSV. All lower-cased.
_DEFAULT_BRIDGES: frozenset = frozenset({
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d",  # Uniswap V2 Router
    "0xe592427a0aece92de3edee1f18e0157c05861564",  # Uniswap V3 Router (SwapRouter)
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45",  # Uniswap V3 Router02
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",  # Uniswap Universal Router
    "0x8731d54e9d02c286767d56ac03e8037c07e01e98",  # Stargate Router
    "0x1a0ad011913a150f69f6a19df447a0cfd9551054",  # Across Protocol
    "0x3154cf16ccdb4c6d922629664174b904d80f2c35",  # Base Bridge L1
    "0x6b75d8af000000e20b7a7ddf000ba900b4009a80",  # zkSync Era Bridge
    "0xa0c68c638235ee32657e8f720a23cec1bfc77c77",  # Arbitrum Inbox
    "0x1111111254eeb25477b68fb85ed929f73a960582",  # 1inch v5 Aggregation Router
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff",  # 0x Exchange Proxy
})


def _load_chains() -> list:
    """Discover CHAIN_<NAME>_URL env vars and build a list of ChainSpec.

    Example .env:
        CHAIN_ETH_URL=wss://eth-mainnet.g.alchemy.com/v2/...
        CHAIN_BASE_URL=wss://base-mainnet.g.alchemy.com/v2/...
        CHAIN_BSC_URL=wss://bsc-dataseed.binance.org/ws

    Returns an empty list if no chain-specific vars are set; the Daemon
    falls back to single-chain mode (Config.chain_type + Config.wss_url).
    """
    chains: list[ChainSpec] = []
    for key, val in sorted(os.environ.items()):
        m = _CHAIN_ENV_PATTERN.match(key)
        if not m:
            continue
        url = (val or "").strip()
        if not url:
            continue
        chains.append(ChainSpec.for_name(m.group(1), url))
    return chains


def _load_bridge_addresses() -> frozenset:
    """Merge BRIDGE_ADDRESSES env CSV with the default starter set."""
    extras: set[str] = set()
    raw = os.getenv("BRIDGE_ADDRESSES", "").strip()
    if raw:
        for chunk in raw.split(","):
            a = chunk.strip().lower()
            if a:
                extras.add(a)
    return _DEFAULT_BRIDGES | frozenset(extras)


def _load_wss_fallback_urls() -> list:
    """Parse WSS_FALLBACK_URLS as a CSV. Each entry is decrypted via the
    SecurityVault before being handed to the transport so operators can
    rotate provider keys without leaking them to the .env in plaintext.

    Example .env line:
        WSS_FALLBACK_URLS=wss://mainnet.infura.io/ws/v3/KEY,wss://eth.llamarpc.com
    """
    raw = (os.getenv("WSS_FALLBACK_URLS", "") or "").strip()
    if not raw:
        return []
    from core.security import Vault
    out: list[str] = []
    for chunk in raw.split(","):
        u = chunk.strip()
        if not u:
            continue
        decrypted = Vault.decrypt(u)
        out.append(decrypted)
    return out


def _load_vip_wallets(env_path: Optional[Path]) -> frozenset:
    """Merge VIP wallets from VIP_WALLETS (CSV) + VIP_WALLETS_FILE (JSON list).

    Resolution:
      • VIP_WALLETS env var  : comma-separated addresses (highest signal,
                               lowest friction; good for ad-hoc additions).
      • VIP_WALLETS_FILE env : path to a JSON file whose top level is a
                               list of strings.
      • <env_path>/targets.json : convention-over-config — if present
                               next to the .env, auto-load. Lets the
                               operator keep a long-lived file without
                               another env var.

    All addresses are lower-cased so the worker can match in O(1)
    against tx.from_addr.lower() / tx.to_addr.lower().
    """
    addrs: set[str] = set()

    raw = os.getenv("VIP_WALLETS", "").strip()
    if raw:
        for chunk in raw.split(","):
            a = chunk.strip().lower()
            if a:
                addrs.add(a)

    candidates: list[Path] = []
    explicit = os.getenv("VIP_WALLETS_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    if env_path is not None:
        candidates.append(Path(env_path).resolve().parent / "targets.json")

    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, list):
            for item in data:
                if isinstance(item, str) and item.strip():
                    addrs.add(item.strip().lower())

    return frozenset(addrs)

def _load_targets(env_path: Optional[Path]) -> dict:
    """v7.0 Watchlist loader. Maps addresses to aliases."""
    targets = {}
    candidates = []
    if env_path is not None:
        candidates.append(Path(env_path).resolve().parent / "targets.json")
    
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    targets[k.lower()] = str(v)
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "address" in item and "alias" in item:
                        targets[item["address"].lower()] = str(item["alias"])
        except Exception:
            continue
    return targets


def _load_dotenv(path: Path) -> None:
    """Minimal .env parser — zero external dependencies."""
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = value
