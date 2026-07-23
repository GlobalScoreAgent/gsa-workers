"""EVM network definitions: public RPCs + block time for lookback."""

NETWORKS = {
    "ethereum": {
        "name": "Ethereum Mainnet",
        "evm_chain_id": 1,
        "block_time_sec": 12.0,
        # Cloudflare first (getLogs works with max range 800 from GHA).
        # publicnode last: frequent 403 on eth_getLogs from GitHub runners.
        "rpcs": [
            "https://cloudflare-eth.com",
            "https://eth.drpc.org",
            "https://ethereum.publicnode.com",
        ],
        "log_chunk_blocks": 800,
        "log_chunk_max": 800,
        "wallet_batch_size": 25,
    },
    "base": {
        "name": "Base Mainnet",
        "evm_chain_id": 8453,
        "block_time_sec": 2.0,
        "rpcs": [
            "https://base-rpc.publicnode.com",
            "https://base.drpc.org",
            "https://mainnet.base.org",
        ],
    },
    "arbitrum": {
        "name": "Arbitrum One",
        "evm_chain_id": 42161,
        "block_time_sec": 0.25,
        "rpcs": [
            "https://arb1.arbitrum.io/rpc",
            "https://arbitrum.drpc.org",
            "https://arbitrum-one.publicnode.com",
        ],
    },
    "polygon": {
        "name": "Polygon PoS",
        "evm_chain_id": 137,
        "block_time_sec": 2.0,
        "rpcs": [
            "https://polygon.drpc.org",
            "https://polygon-bor.publicnode.com",
            "https://polygon-rpc.com",
        ],
    },
    "bsc": {
        "name": "BNB Smart Chain",
        "evm_chain_id": 56,
        "block_time_sec": 3.0,
        "rpcs": [
            "https://bsc-dataseed.bnbchain.org",
            "https://bsc.drpc.org",
            "https://bsc-dataseed1.defibit.io",
        ],
    },
    "celo": {
        "name": "Celo Mainnet",
        "evm_chain_id": 42220,
        "block_time_sec": 5.0,
        "rpcs": [
            "https://forno.celo.org",
            "https://celo.drpc.org",
            "https://rpc.ankr.com/celo",
        ],
    },
    "gnosis": {
        "name": "Gnosis Chain",
        "evm_chain_id": 100,
        "block_time_sec": 5.0,
        "rpcs": [
            "https://rpc.gnosischain.com",
            "https://gnosis-rpc.publicnode.com",
            "https://rpc.ankr.com/gnosis",
        ],
    },
    "xlayer": {
        "name": "X Layer Mainnet",
        "evm_chain_id": 196,
        "block_time_sec": 3.0,
        "rpcs": [
            "https://rpc.xlayer.tech",
            "https://xlayerrpc.okx.com",
            "https://xlayer.drpc.org",
        ],
    },
}

EVM_CHAIN_ID_TO_SLUG = {
    net["evm_chain_id"]: slug for slug, net in NETWORKS.items()
}
