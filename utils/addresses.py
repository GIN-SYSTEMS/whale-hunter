"""
utils/addresses.py — Public on-chain address databases.

All addresses are publicly available blockchain identifiers.
No personal data is stored — GDPR compliant by design.
Sources: Etherscan labels, OFAC SDN list, public forensics reports.
"""

from __future__ import annotations

# ── Government / Institutional Seizure Wallets ───────────
# Public record — US DOJ, German BKA, UK NCA, etc.
GOVERNMENT_WALLETS: frozenset[str] = frozenset({
    # US DOJ / FBI seizure addresses (public court filings)
    "0xb76f765a785eca438e1d95f594490088afaf9acc",
    "0x681297e671927945e28b2ef10dce0a6728de4a3c",
    "0x48549a34ae37b12f6a30566245176994e17c6b4a",
    "0x9f4cda013e354b8fc285bf4b9a60460cee7f7ea9",
    # German BKA (Bundeskriminalamt) — public blockchain forensics
    "0xbc4b9527d3e1e52e4f38fcbbe6b6622cc34e2c11",
    "0x1da5821544e25c636c1417ba96ade4cf6d2f9b5a",
    # UK NCA — public seizure records
    "0x7db418c0a5050000fb00200600b07c02e0090600",
    # Ukrainian Government donation wallet (public)
    "0x165cd37b4c644c2921454429e7f9358d18a45e14",
    # El Salvador BTC/ETH treasury (public)
    "0xe38e1aa0c0c10715a5e2af0176e6a0e36cb10515",
})

# ── Known Dormant / Genesis-Era Wallets ──────────────────
# Wallets inactive for 5+ years, publicly tracked by chain analysis
DORMANT_WALLETS: frozenset[str] = frozenset({
    # Genesis block allocations (public Ethereum history)
    "0xde0b295669a9fd93d5f28d9ec85e40f4cb697bae",
    "0x47ac0fb4f2d84898e4d9e7b4dab3c24507a6d503",
    "0xbe0eb53f46cd790cd13851d5eff43d12404d33e8",
    "0x1b3cb81e51011b549d78bf720b0d924ac763a7c2",
    "0xc098b2a3aa256d2140208c3de6543aaef5cd3a94",
    "0x176f3dab24a159341c0509bb36b833e7fdd0a132",
    "0x3bfc20f0b9afcace800d73d2191166ff16540258",
    "0x189b9cbd4aff470af2c0102f365fc1823d857965",
    "0x220866b1a2219f40e72f5c628b65d54268ca3a9d",
    "0xbddf00563c9130ecac2f1f3a4e3b5e3109bb5b14",
    # ICO-era whales known to be dormant
    "0xab7c74abc0c4d48d1bdad5dcb26153fc8780f83e",
    "0x6f46cf5569aefa1acc1009290c8e043747172d89",
    "0x90e63c3d53e0ea496845b7a03ec7548b70014a91",
    "0x53d284357ec70ce289d6d64134dfac8e511c8a3d",
    "0xfe9e8709d3215310075d67e3ed32a380ccf451c8",
})

# ── Known Exchange Hot Wallets ───────────────────────────
EXCHANGE_WALLETS: frozenset[str] = frozenset({
    # Binance
    "0x28c6c06298d514db089934071355e5743bf21d60",
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549",
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d",
    # Coinbase
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3",
    "0x503828976d22510aad0201ac7ec88293211d23da",
    "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43",
    # Kraken
    "0x2910543af39aba0cd09dbb2d50200b3e800a63d2",
    "0x0a869d79a7052c7f1b55a8ebabbea3420f0d1e13",
    # OKX
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b",
    "0x236f9f97e0e62388479bf9e5ba4889e46b0273c3",
})

# ── Tornado Cash Contracts (OFAC sanctioned, public) ─────
TORNADO_CONTRACTS: frozenset[str] = frozenset({
    "0xd90e2f925da726b50c4ed8d0fb90ad053324f31b",  # 0.1 ETH
    "0x910cbd523d972eb0a6f4cae4618ad62622b39dbf",  # 10 ETH
    "0xa160cdab225685da1d56aa342ad8841c3b53f291",  # 100 ETH
    "0x47ce0c6ed5b0ce3d3a51fdb1c52dc66a7c3c2936",  # 1000 ETH
    "0x722122df12d4e14e13ac3b6895a86e84145b6967",  # Router
    "0xba214c1c1928a32bffe790263e38b4af9bfcd659",  # Proxy
})

# ── Known Hack / Exploit Addresses (public forensics) ────
HACK_ADDRESSES: frozenset[str] = frozenset({
    # Ronin Bridge hack (public — Lazarus Group attributed)
    "0x098b716b8aaf21512996dc57eb0615e2383e2f96",
    # Wormhole exploit (public)
    "0x629e7da20197a5429d30da36e77d06cdf796b71a",
    # Nomad Bridge hack (public)
    "0x56d811088235f11c8920698a204a5010a788f4b3",
    # Harmony Bridge hack (public)
    "0x0d043128146654c7683fbf30ac98d7b2285ded00",
    # Euler Finance exploit (public)
    "0xb66cd966670d962c227b3eaba30a872dbfb995db",
    # Poly Network hack (public)
    "0xc8a65fadf0e0ddaf421f28feab69bf6e2e589963",
    # Wintermute hack (public)
    "0xe74b28c2eae8679e3ccc3a94d5d0de83ccb84705",
})

# ── ERC-20 Token Contracts (Ethereum mainnet) ────────────
# symbol → {address, decimals, price_usd}
# price_usd is a reasonable default; update via ETH_PRICE_USD env var for WBTC.
TOKEN_CONTRACTS: dict[str, dict] = {
    # Stablecoins
    "0xdac17f958d2ee523a2206206994597c13d831ec7": {
        "symbol": "USDT", "decimals": 6,  "price_usd": 1.0,
    },
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": {
        "symbol": "USDC", "decimals": 6,  "price_usd": 1.0,
    },
    "0x6b175474e89094c44da98b954eedeac495271d0f": {
        "symbol": "DAI",  "decimals": 18, "price_usd": 1.0,
    },
    # High-cap tokens
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": {
        "symbol": "WBTC", "decimals": 8,  "price_usd": 65_000.0,
    },
    "0x514910771af9ca656af840dff83e8264ecf986ca": {
        "symbol": "LINK", "decimals": 18, "price_usd": 15.0,
    },
}

# Fast O(1) contract lookup set
TOKEN_CONTRACT_ADDRS: frozenset[str] = frozenset(TOKEN_CONTRACTS.keys())

# ERC-20 Transfer(address indexed,address indexed,uint256) selector
ERC20_TRANSFER_SELECTOR = "0xa9059cbb"

# ── All monitored sets combined for fast lookup ──────────
ALL_WATCHED: frozenset[str] = (
    GOVERNMENT_WALLETS
    | DORMANT_WALLETS
    | EXCHANGE_WALLETS
    | TORNADO_CONTRACTS
    | HACK_ADDRESSES
)
