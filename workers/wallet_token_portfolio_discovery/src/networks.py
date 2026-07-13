"""Chain metadata for portfolio calc (DeFiLlama keys + native symbols)."""

from __future__ import annotations

# internal erc_8004.chains.id → pricing / display metadata
CHAIN_META: dict[int, dict[str, str]] = {
    1: {
        "name": "ethereum",
        "llama_chain": "ethereum",
        "native_symbol": "ETH",
        "native_llama": "coingecko:ethereum",
        "decimals": "18",
    },
    2: {
        "name": "base",
        "llama_chain": "base",
        "native_symbol": "ETH",
        "native_llama": "coingecko:ethereum",
        "decimals": "18",
    },
    3: {
        "name": "polygon",
        "llama_chain": "polygon",
        "native_symbol": "POL",
        "native_llama": "coingecko:matic-network",
        "decimals": "18",
    },
    4: {
        "name": "bsc",
        "llama_chain": "bsc",
        "native_symbol": "BNB",
        "native_llama": "coingecko:binancecoin",
        "decimals": "18",
    },
    6: {
        "name": "arbitrum",
        "llama_chain": "arbitrum",
        "native_symbol": "ETH",
        "native_llama": "coingecko:ethereum",
        "decimals": "18",
    },
    8: {
        "name": "celo",
        "llama_chain": "celo",
        "native_symbol": "CELO",
        "native_llama": "coingecko:celo",
        "decimals": "18",
    },
    9: {
        "name": "gnosis",
        "llama_chain": "xdai",
        "native_symbol": "xDAI",
        "native_llama": "coingecko:xdai",
        "decimals": "18",
    },
}
