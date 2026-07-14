# Wallet LP positions discovery

> Project context: [AGENTS.md](../../AGENTS.md) · [Supabase map](../../docs/SUPABASE.md) · [Architecture](../../docs/ARCHITECTURE.md)

Initial LP snapshot fill: UniV3 / Pancake NFT managers + classic pools from `wallets.lp_pools` → amounts → USD (DeFiLlama, then `wallets.token_prices`) → replace upsert into `wallets.wallet_lp_positions`.

Does **not** compute WAMI. A separate 15-day refresh worker (uses `calculated_at`) is planned later.

## Eligibility

```sql
does_need_lp_discovery IS DISTINCT FROM FALSE
AND does_need_portfolio_discovery = FALSE
AND COALESCE(has_portfolio_discovery_error, FALSE) IS NOT TRUE
AND chains.subdomain_alchemy IS NOT NULL
```

Trigger sets the LP flag when portfolio discovery completes successfully.

## Pipeline

1. Claim `wallet_transactions` rows
2. **Step 1 (NFT):** NFPM `balanceOf` → `tokenOfOwnerByIndex` → `positions` → factory `getPool` + `slot0` → liquidity amounts
3. **Step 2 (classic):** Active `wallets.lp_pools` → LP + gauge `balanceOf` → reserve share amounts
4. Price underlyings → `wallets.wallet_lp_positions_upsert` (delete+insert; stamps `calculated_at`)
5. Mark LP discovery done (flag `FALSE` even on error)

Empty LP wallet still completes successfully with `inserted=0`.

### Chains / protocols (v1)

| Chain | NFT | Classic (`lp_pools`) |
|---|---|---|
| Ethereum | Uniswap V3 | — |
| Base | Uniswap V3 | Aerodrome V1 seeds (`active`) |
| Arbitrum | Uniswap V3 | — |
| BSC | PancakeSwap V3 | — |

Add/disable classic targets with `INSERT`/`UPDATE wallets.lp_pools SET active=…` — no worker redeploy.

## Environment

| Variable | Default | Description |
|---|---|---|
| `SUPABASE_DB_URL` | required | Postgres |
| `ALCHEMY_FREE_KEY` | required | Alchemy (`ALCHEMY_KEY` fallback) |
| `WORKER_ID` | `discovery-a` | Suffix for claimed_by |
| `CONCURRENCY` | 5 | Parallel rows |
| `CLAIM_BATCH_SIZE` | 25 | |
| `CLAIM_STALE_SECONDS` | 7200 | |
| `MAX_RUNTIME_SECONDS` | 19800 | |

## Local run

```bash
cd workers/wallet_lp_positions_discovery
uv sync
uv run python job.py
```

Schema must be applied first (`gsa-supabase-schema` migration `20260714000000_wallet_lp_positions_discovery`).
