# AGENTS.md — working on gsa-workers

Entry point for AI agents (and humans) changing GitHub Actions wallet workers.

## What this repo is

**Python 3.12** batch jobs on **GitHub Actions**. Most claim rows from Supabase Postgres (`erc_8004.wallets`), query 8 EVM chains over HTTP, save JSON, then call **inline SQL snapshot** functions so status becomes `Processed`. There is also a **reference-data** worker (CEX addresses) that fetches an external API and calls one upsert RPC — no claim loop.

- **Not** Edge Functions / supabase-js in the hot path
- **Not** Cloudflare Workers for these pipelines
- Schema / RPCs live in sibling repo **`gsa-supabase-schema`**

## Read in this order

1. [README.md](./README.md) — workers table, secrets, local run
2. [docs/PROCESSES.md](./docs/PROCESSES.md) — catalog of all live pipelines
3. [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) — claim → RPC → save → snapshot
4. [docs/SUPABASE.md](./docs/SUPABASE.md) — columns, RPCs, monitoring SQL
5. The worker README for the job you touch (`workers/<name>/README.md`)
6. That worker’s `src/db.py` and `job.py` (code of truth)

Ops / stuck wallets: [docs/OPS.md](./docs/OPS.md). Deprecations: [docs/DEPRECATION.md](./docs/DEPRECATION.md).  
LP discovery is live; 15-day refresh still pending: [docs/PENDING_LP_POSITIONS.md](./docs/PENDING_LP_POSITIONS.md).

## Hard rules

1. **Eligibility** (claim workers) uses `is_valid_*` + `*_next_eligible_at <= NOW()`, not legacy “status + day window” alone.
2. **Claim workers** pipeline is always claim → RPC → save → `wallet_apply_*_snapshot` → `Processed`. Do not reintroduce pg_cron for those snapshots. **Reference-data:** `cex_addresses_import` (Dune → upsert); `token_prices_import` (DexScreener/CoinGecko → upsert → apply hits → `mark_price_misses` for Dex+CG fails).
3. **Do not revive** deprecated cron jobs listed in [DEPRECATION.md](./docs/DEPRECATION.md).
4. DB helpers are **copy-pasted** per worker (`src/db.py`). If you change reconnect/retry or claim SQL, update **all claim-based workers** unless the change is worker-specific.
5. Snapshot / upsert SQL changes belong in **`gsa-supabase-schema`** migrations (+ `supabase/scripts/`), then deploy to prod before relying on new worker behavior.
6. Prefer fixing workers so they **continue** on transient DB errors (retries + loop continue) rather than exiting 1 on the first SSL drop.

## Workers cheat sheet

| Folder | Workflow | Snapshot / upsert RPC | Destination |
|---|---|---|---|
| `wallet_nonce_balance_daily` | `wallet-nonce-balance-daily.yml` (matrix a/b) | `wallet_apply_daily_snapshot` | `wallet_transactions`, `chain_nonces` |
| `owner_wallet_nonce_balance_monthly` | `owner-wallet-nonce-balance-monthly.yml` | `wallet_apply_monthly_snapshot` | `wallet_owner_details` |
| `owner_wallet_origin` | `owner-wallet-origin.yml` | `wallet_apply_owner_history_snapshot` | `wallet_owner_details.first_transaction_at` |
| `cex_addresses_import` | `cex-addresses-import.yml` | `wallets.cex_addresses_upsert` | `wallets.cex_addresses` |
| `token_prices_import` | `token-prices-import.yml` | `token_prices_upsert` + `apply_prices` + `mark_price_misses` | `wallets.token_prices` → positions |
| `wallet_token_contracts_discovery` | `wallet-token-contracts-discovery.yml` | `wallets.wallet_token_contracts_upsert` | `wallets.wallet_token_contracts` |
| `wallet_token_portfolio_discovery` | `wallet-token-portfolio-discovery.yml` | `wallets.wallet_token_positions_insert` | `wallets.wallet_token_positions` (fungible) |
| `wallet_lp_positions_discovery` | `wallet-lp-positions-discovery.yml` | `wallets.wallet_lp_positions_upsert` | `wallets.wallet_lp_positions` (NFT + classic LP) |

LP 15-day refresh worker: **not built** — see [docs/PENDING_LP_POSITIONS.md](./docs/PENDING_LP_POSITIONS.md).

## How to validate a change

1. Local: `cd workers/<name>`, `uv sync`, `uv run python job.py` with `SUPABASE_DB_URL` (+ `ALCHEMY_KEY` / `ALCHEMY_FREE_KEY`, `DUNE_KEY`, or `COINGECKO_KEY` as needed).
2. Or GitHub Actions → workflow → **Run workflow** (`workflow_dispatch`).
3. Logs: look for `Claimed batch`, `Reconnecting to Postgres`, `Claim failed; will retry`, `Save/snapshot failed` (claim workers), Dune/CEX upsert messages, token-price enrich (`dex=`/`cg=`/`miss=`), or `Done wt_id=` (contracts / portfolio / LP discovery).
4. SQL: eligible counts and stuck `Completed` queries in [docs/SUPABASE.md](./docs/SUPABASE.md); CEX / token-prices / discovery monitoring in the same doc.

## When to touch which repo

| Change | Repo |
|---|---|
| Claim SQL, retries, job loop, RPC clients, GHA env | **gsa-workers** |
| `wallet_apply_*_snapshot`, `wallets.cex_addresses_upsert`, `wallets.token_prices_upsert`, `wallet_token_positions_apply_prices`, `wallet_token_positions_mark_price_misses`, `wallet_token_contracts_upsert`, `wallet_token_positions_insert`, `wallet_lp_positions_upsert`, `lp_pools`, chain price subdomains, triggers, indexes, `next_eligible_at` / discovery flags | **gsa-supabase-schema** |
| Deploy order | Schema first (if needed) → push worker → `workflow_dispatch` |
