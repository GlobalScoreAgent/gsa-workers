# Wallet LP positions discovery

> Project context: [AGENTS.md](../../AGENTS.md) · [Process catalog](../../docs/PROCESSES.md) · [Supabase map](../../docs/SUPABASE.md) · [Architecture](../../docs/ARCHITECTURE.md)

**Status: live** (cron `0/6/12/18` UTC + `workflow_dispatch`).

Initial LP / concentrated-liquidity snapshot fill:

1. UniV3 / Pancake **NFT** managers (code config)
2. Classic ERC-20 LP + gauge from **`wallets.lp_pools`** (`active` flag)

→ amounts → USD (DeFiLlama, then `wallets.token_prices`) → replace upsert into `wallets.wallet_lp_positions`.

Does **not** compute WAMI / HUMI. A separate 15-day refresh worker (uses `calculated_at`) is still planned — [PENDING_LP_POSITIONS.md](../../docs/PENDING_LP_POSITIONS.md).

## Eligibility

```sql
does_need_lp_discovery IS DISTINCT FROM FALSE
AND does_need_portfolio_discovery = FALSE
AND COALESCE(has_portfolio_discovery_error, FALSE) IS NOT TRUE
AND chains.subdomain_alchemy IS NOT NULL
```

Trigger `trg_wallet_transactions_lp_flag_bu` sets the LP flag when portfolio discovery completes successfully. Migration also **backfills** rows that already had portfolio done.

## Pipeline

1. Claim `wallet_transactions` rows (soft lock `lp_discovery_claimed_at` / `claimed_by`)
2. **Step 1 (NFT):** NFPM `balanceOf` → `tokenOfOwnerByIndex` → `positions` → factory `getPool` + `slot0` → liquidity → token amounts
3. **Step 2 (classic):** Active `wallets.lp_pools` for that `chain_id` → LP + gauge `balanceOf` → reserve share amounts
4. Price underlyings → `wallets.wallet_lp_positions_upsert` (DELETE+INSERT for that wallet+chain; stamps `calculated_at`)
5. Mark LP discovery done (`does_need_lp_discovery = FALSE` even on error, with error columns)

Empty LP wallet still completes successfully (`inserted=0`). **Most wallets have no UniV3 NFT / classic balance**, so queue progress ≫ row count in `wallet_lp_positions`.

### Chains / protocols (v1 extractors)

| Chain id | Chain | NFT | Classic (`lp_pools`) |
|---|---|---|---|
| 1 | Ethereum | Uniswap V3 | — |
| 2 | Base | Uniswap V3 | Aerodrome V1 (seeded, `active`) |
| 4 | BNB | PancakeSwap V3 | — |
| 6 | Arbitrum | Uniswap V3 | — |

Other Alchemy chains in the claim queue (Polygon, Gnosis, Celo, X Layer, …) still get claimed after portfolio: both steps no-op → empty upsert → mark done. That is expected until coverage is added.

Add/disable classic targets with `INSERT` / `UPDATE wallets.lp_pools SET active=…` — no worker redeploy.

### Row identity / PK sentinels

PK: `(wallet_id, chain_id, position_kind, nft_manager_address, token_id, pool_address)`  
FKs: `wallet_id → erc_8004.wallets`, `chain_id → erc_8004.chains`.

Classic rows use sentinels: `nft_manager_address = ''`, `token_id = -1`.

Schema: `gsa-supabase-schema` migrations `20260714000000_wallet_lp_positions_discovery` + `20260714010000_wallet_lp_positions_pk_fk`.

## Module layout

| File | Role |
|---|---|
| `job.py` | Claim loop, concurrency, runtime budget |
| `src/db.py` | Claim / load pools & token_prices / upsert / mark done\|error |
| `src/nft_lp.py` | Step 1 UniV3-style NFT path |
| `src/classic_lp.py` | Step 2 classic LP + gauge |
| `src/pricing.py` | DeFiLlama + `token_prices` fallback |
| `src/lp_calc.py` | Orchestrates extract + price |
| `src/networks.py` | NFPM / factory map, Llama chain keys |
| `src/rpc.py` / `univ3_math.py` | eth_call + Multicall3, liquidity math |

## Environment

| Variable | Default | Description |
|---|---|---|
| `SUPABASE_DB_URL` | required | Postgres |
| `ALCHEMY_FREE_KEY` | required | Alchemy (`ALCHEMY_KEY` fallback) |
| `WORKER_ID` | `discovery-a` | Suffix for claimed_by |
| `CONCURRENCY` | 5 | Parallel rows |
| `CLAIM_BATCH_SIZE` | 25 | |
| `CLAIM_STALE_SECONDS` | 7200 | |
| `MAX_RUNTIME_SECONDS` | 19800 | Soft stop (~5.5h) |

Workflow: `.github/workflows/wallet-lp-positions-discovery.yml` (`timeout-minutes: 360`).

## Local run

```powershell
cd workers/wallet_lp_positions_discovery
copy .env.example .env
# Set SUPABASE_DB_URL and ALCHEMY_FREE_KEY
uv sync
uv run python job.py
```

## Monitoring / reset

See [docs/SUPABASE.md](../../docs/SUPABASE.md) (LP section). Full rediscovery: `wallet_lp_positions_discovery_reset.sql` (**ask before TRUNCATE** in prod) then `workflow_dispatch`.
