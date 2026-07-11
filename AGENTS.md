# AGENTS.md — working on gsa-workers

Entry point for AI agents (and humans) changing GitHub Actions wallet workers.

## What this repo is

**Python 3.12** batch jobs on **GitHub Actions**. Most claim rows from Supabase Postgres (`erc_8004.wallets`), query 8 EVM chains over HTTP, save JSON, then call **inline SQL snapshot** functions so status becomes `Processed`. There is also a **reference-data** worker (CEX addresses) that fetches an external API and calls one upsert RPC — no claim loop.

- **Not** Edge Functions / supabase-js in the hot path
- **Not** Cloudflare Workers for these pipelines
- Schema / RPCs live in sibling repo **`gsa-supabase-schema`**

## Read in this order

1. [README.md](./README.md) — workers table, secrets, local run
2. [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) — claim → RPC → save → snapshot
3. [docs/SUPABASE.md](./docs/SUPABASE.md) — columns, RPCs, monitoring SQL
4. The worker README for the job you touch (`workers/<name>/README.md`)
5. That worker’s `src/db.py` and `job.py` (code of truth)

Ops / stuck wallets: [docs/OPS.md](./docs/OPS.md). Deprecations: [docs/DEPRECATION.md](./docs/DEPRECATION.md).

## Hard rules

1. **Eligibility** uses `is_valid_*` + `*_next_eligible_at <= NOW()`, not legacy “status + day window” alone.
2. **Pipeline** is always claim → RPC → save → `wallet_apply_*_snapshot` → `Processed`. Do not reintroduce pg_cron for those snapshots.
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

## How to validate a change

1. Local: `cd workers/<name>`, `uv sync`, `uv run python job.py` with `SUPABASE_DB_URL` (+ `ALCHEMY_KEY` or `DUNE_KEY` as needed).
2. Or GitHub Actions → workflow → **Run workflow** (`workflow_dispatch`).
3. Logs: look for `Claimed batch`, `Reconnecting to Postgres`, `Claim failed; will retry`, `Save/snapshot failed` (claim workers) or Dune fetch / upsert messages (`cex_addresses_import`).
4. SQL: eligible counts and stuck `Completed` queries in [docs/SUPABASE.md](./docs/SUPABASE.md); CEX monitoring in the same doc.

## When to touch which repo

| Change | Repo |
|---|---|
| Claim SQL, retries, job loop, RPC clients, GHA env | **gsa-workers** |
| `wallet_apply_*_snapshot`, `wallets.cex_addresses_upsert`, triggers, indexes, `next_eligible_at` columns | **gsa-supabase-schema** |
| Deploy order | Schema first (if needed) → push worker → `workflow_dispatch` |
