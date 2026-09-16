"""Chain / NFPM / factory / DeFiLlama metadata for holdings discovery."""

from __future__ import annotations

# erc_8004.chains.id → pricing / display / llama keys
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
        "native_llama": "coingecko:polygon-ecosystem-token",
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

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"

# chain_id → protocol → NonfungiblePositionManager
NFPM_BY_CHAIN: dict[int, dict[str, str]] = {
    1: {"uniswap_v3": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88"},
    2: {"uniswap_v3": "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1"},
    4: {"pancakeswap_v3": "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364"},
    6: {"uniswap_v3": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88"},
}

# Fallback factories if NFPM.factory() fails
FACTORY_FALLBACK: dict[str, str] = {
    "uniswap_v3:1": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
    "uniswap_v3:2": "0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
    "uniswap_v3:6": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
    "pancakeswap_v3:4": "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865",
}
