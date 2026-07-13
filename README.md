# GSA Workers

Unified Python batch workers for [Global Score Agent](https://www.globalscoreagent.com/), run via GitHub Actions against Supabase Postgres.

**For AI agents:** start at [AGENTS.md](./AGENTS.md). Architecture and DB maps: [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md), [docs/SUPABASE.md](./docs/SUPABASE.md), [docs/OPS.md](./docs/OPS.md).

## Workers

| Worker | Schedule (UTC) | Eligibility | Description |
|---|---|---|---|
| [`wallet_nonce_balance_daily`](./workers/wallet_nonce_balance_daily/README.md) | 0, 6, 12, 18h (matrix `worker-a`/`worker-b`) | `is_valid_..._daily` + `import_nonce_and_balance_daily_next_eligible_at` | Balance + nonce → daily JSON → `wallet_apply_daily_snapshot` |
| [`owner_wallet_origin`](./workers/owner_wallet_origin/README.md) | 0, 6, 12, 18h | monthly `is_valid` + `import_wallet_history_next_eligible_at` | First on-chain activity → history JSON → `wallet_apply_owner_history_snapshot` |
| [`owner_wallet_nonce_balance_monthly`](./workers/owner_wallet_nonce_balance_monthly/README.md) | 0, 6, 12, 18h | `is_valid_..._monthly` + `import_nonce_and_balance_monthly_next_eligible_at` | Balance + nonce (30d) → monthly JSON → `wallet_apply_monthly_snapshot` |
| [`cex_addresses_import`](./workers/cex_addresses_import/README.md) | 1st & 16th 00:00 (~every 15 days) | n/a (reference data) | Dune CEX list → `wallets.cex_addresses_upsert` |
| [`token_prices_import`](./workers/token_prices_import/README.md) | **paused** (manual only) | n/a (reference data) | Dune token prices → `wallets.token_prices_upsert` |
| [`wallet_token_contracts_discovery`](./workers/wallet_token_contracts_discovery/README.md) | 0, 6, 12, 18h | `wallet_transactions.does_need_discovery_contracts` + `chains.subdomain_alchemy` | Alchemy ERC-20 balances → `wallet_token_contracts_upsert` |

## Common pipeline (claim workers)

```
claim (Pending, next_eligible_at += CLAIM_STALE_SECONDS)
  → RPC (8 chains, public then Alchemy)
    → save (Completed|Error + schedule next run)
  → wallet_apply_*_snapshot → Processed
```

Reference-data (`cex_addresses_import`, `token_prices_import`): Dune fetch → one upsert RPC. Details: [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md). Column/RPC inventory: [docs/SUPABASE.md](./docs/SUPABASE.md).

## Secrets

| Secret | Required | Role |
|---|---|---|
| `SUPABASE_DB_URL` | Yes | Postgres pooler DSN |
| `ALCHEMY_KEY` | Recommended | Alchemy fallback after public RPCs (claim workers) |
| `ALCHEMY_FREE_KEY` | For token contracts discovery | Alchemy Token API (`getTokenBalances`) |
| `DUNE_KEY` | For CEX / token-prices import | Dune Analytics API key |

## CI defaults (workflows)

| Worker | CONCURRENCY | CLAIM_BATCH_SIZE | CLAIM_STALE_SECONDS | MAX_RUNTIME_SECONDS |
|---|---|---|---|---|
| daily | 20 | 200 | 7200 | 19800 |
| origin | 4 | 50 | 7200 | 19800 |
| monthly | 20 | 200 | 7200 | 19800 |
| cex import | n/a | n/a | n/a | GHA timeout 30m |
| token prices | n/a | n/a | n/a | GHA timeout 30m |
| token contracts discovery | 10 | 50 | 7200 | 19800 |

Daily also sets `WORKER_ID` to `worker-a` or `worker-b`. Origin/monthly set `SKIP_ELIGIBLE_COUNT=1`.

Manual run: **Actions** → pick workflow → **Run workflow**.

## Local development

```powershell
cd workers/<worker_name>
copy .env.example .env
# Set SUPABASE_DB_URL and ALCHEMY_KEY or DUNE_KEY as needed

uv sync
uv run python job.py
```

## Repository layout

```
gsa-workers/
├── AGENTS.md
├── README.md
├── docs/
│   ├── ARCHITECTURE.md
│   ├── SUPABASE.md
│   ├── OPS.md
│   └── DEPRECATION.md
├── workers/
│   ├── wallet_nonce_balance_daily/
│   │   ├── job.py
│   │   ├── README.md
│   │   ├── pyproject.toml
│   │   └── src/          # db, query, rpc, alchemy, networks, address
│   ├── owner_wallet_origin/
│   │   ├── job.py
│   │   ├── scripts/
│   │   └── src/          # db, origin, ...
│   ├── owner_wallet_nonce_balance_monthly/
│   │   ├── job.py
│   │   └── src/
│   ├── cex_addresses_import/
│   │   ├── job.py
│   │   └── src/          # db, dune
│   ├── token_prices_import/
│   │   ├── job.py
│   │   └── src/          # db, dune
│   └── wallet_token_contracts_discovery/
│       ├── job.py
│       └── src/          # db, alchemy_tokens
└── .github/workflows/
    ├── wallet-nonce-balance-daily.yml
    ├── owner-wallet-origin.yml
    ├── owner-wallet-nonce-balance-monthly.yml
    ├── cex-addresses-import.yml
    ├── token-prices-import.yml
    └── wallet-token-contracts-discovery.yml
```

Schema / snapshot SQL: sibling repo **`gsa-supabase-schema`**.

## Deprecation

See [docs/DEPRECATION.md](./docs/DEPRECATION.md) (Cloudflare/Edge Phase 2 + deprecated pg_cron snapshot jobs).
