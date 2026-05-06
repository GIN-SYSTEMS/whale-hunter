"""
utils/explorer.py — chain-aware block-explorer URL builders.

Hoisted from ui/inspect.py in v4.2 so non-UI callers (alerts/telegram.py,
core/daemon.py) can build URLs without importing Textual.

Add a new EVM chain by appending one row to _EXPLORER_BASES — the rest
of the pipeline already speaks Transaction.network and routes correctly.
"""
from __future__ import annotations


_EXPLORER_BASES: dict[str, str] = {
    "ETH":  "https://etherscan.io",
    "BASE": "https://basescan.org",
    "ARB":  "https://arbiscan.io",
    "POLY": "https://polygonscan.com",
    "BSC":  "https://bscscan.com",
    "OPT":  "https://optimistic.etherscan.io",
    "SOL":  "https://solscan.io",
    "BTC":  "https://www.blockchain.com/btc",
}


def explorer_url(chain: str, target: str, kind: str = "address") -> str:
    """Build a block-explorer URL for the given chain and target.

    kind: "address" | "tx"
    Falls back to Etherscan for unrecognised chains.
    """
    base = _EXPLORER_BASES.get((chain or "ETH").upper(), _EXPLORER_BASES["ETH"])
    if kind == "tx":
        return f"{base}/tx/{target}"
    return f"{base}/address/{target}"


def arkham_url(address: str) -> str:
    """Arkham Intelligence Entity ID lookup (chain-agnostic)."""
    return f"https://platform.arkhamintelligence.com/explorer/address/{address}"


def debank_url(address: str) -> str:
    """DeBank multi-chain DeFi profile URL."""
    return f"https://debank.com/profile/{address}"
