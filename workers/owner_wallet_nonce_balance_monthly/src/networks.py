"""EVM network definitions and public RPC endpoints (primary + fallbacks)."""

NETWORKS = {
    "ethereum": {
        "name": "Ethereum Mainnet",
        "chain_id": 1,
        "symbol": "ETH",
        "rpcs": [
            "https://ethereum.publicnode.com",
            "https://eth.drpc.org",
            "https://cloudflare-eth.com",
        ],
    },
    "base": {
        "name": "Base Mainnet",
        "chain_id": 8453,
        "symbol": "ETH",
        "rpcs": [
            "https://mainnet.base.org",
            "https://base-rpc.publicnode.com",
            "https://base.drpc.org",
        ],
    },
    "arbitrum": {
        "name": "Arbitrum One",
        "chain_id": 42161,
        "symbol": "ETH",
        "rpcs": [
            "https://arb1.arbitrum.io/rpc",
            "https://arbitrum.drpc.org",
            "https://arbitrum-one.publicnode.com",
        ],
    },
    "polygon": {
        "name": "Polygon PoS",
        "chain_id": 137,
        "symbol": "POL/MATIC",
        "rpcs": [
            "https://polygon-rpc.com",
            "https://polygon.drpc.org",
            "https://polygon-bor.publicnode.com",
        ],
    },
    "bsc": {
        "name": "BNB Smart Chain",
        "chain_id": 56,
        "symbol": "BNB",
        "rpcs": [
            "https://bsc-dataseed.bnbchain.org",
            "https://bsc.drpc.org",
            "https://bsc-dataseed1.defibit.io",
        ],
    },
    "celo": {
        "name": "Celo Mainnet",
        "chain_id": 42220,
        "symbol": "CELO",
        "rpcs": [
            "https://forno.celo.org",
            "https://celo.drpc.org",
            "https://rpc.ankr.com/celo",
        ],
    },
    "gnosis": {
        "name": "Gnosis Chain",
        "chain_id": 100,
        "symbol": "xDAI",
        "rpcs": [
            "https://rpc.gnosischain.com",
            "https://gnosis-rpc.publicnode.com",
            "https://rpc.ankr.com/gnosis",
        ],
    },
    "xlayer": {
        "name": "X Layer Mainnet",
        "chain_id": 196,
        "symbol": "OKB",
        "rpcs": [
            "https://rpc.xlayer.tech",
            "https://xlayerrpc.okx.com",
            "https://xlayer.drpc.org",
        ],
    },
}

CHAIN_ORDER = [
    "ethereum",
    "base",
    "arbitrum",
    "polygon",
    "bsc",
    "celo",
    "gnosis",
    "xlayer",
]
