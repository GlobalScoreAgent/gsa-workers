# GSA Workers

Unified Python batch workers for [Global Score Agent](https://www.globalscoreagent.com/), run via GitHub Actions.

## Workers

| Worker | Schedule | Description |
|---|---|---|
| `wallet_nonce_balance_daily` | 4×/day UTC (0, 6, 12, 18h) | Balance + nonce across 8 EVM chains → `erc_8004.wallets` |

## wallet_nonce_balance_daily

Reads pending wallets from `erc_8004.wallets`:

- `is_valid_import_current_nonce_and_balance_daily = true`
- `import_nonce_and_balance_daily_at` is NULL or before today (UTC)

Writes:

- `import_current_nonce_and_balance_daily_json`
- `import_nonce_and_balance_daily_last_status` (`Completed` | `Error`)
- `import_nonce_and_balance_daily_at`

RPC strategy per chain:

1. Public RPC fallbacks (`networks.py`)
2. Alchemy JSON-RPC batch fallback (`alchemy.py`, pattern from `wallet-transactional-current-batch`)
3. `subdomain_alchemy` loaded from `erc_8004.chains`

Wallets with `Error` are **not** retried the same UTC day.

### Local development

```powershell
cd workers/wallet_nonce_balance_daily
copy .env.example .env
# Edit .env with SUPABASE_DB_URL and ALCHEMY_KEY

uv sync
uv run python job.py
```

### GitHub Actions secrets

| Secret / Var | Required | Default |
|---|---|---|
| `SUPABASE_DB_URL` | Yes | Postgres connection string (pooler) |
| `ALCHEMY_KEY` | Recommended | Alchemy fallback |
| `CONCURRENCY` (var) | No | `15` (max 20) |
| `BATCH_SIZE` (var) | No | `500` |
| `MAX_RUNTIME_SECONDS` (var) | No | `19800` (5.5h) |

Manual run: **Actions** → **Wallet nonce balance daily** → **Run workflow**.

### Phase 2 (deprecation)

After this worker is stable in production:

- Deprecate Cloudflare Worker `wallet-snapshot` (`gsa-cloudflare-workers`)
- Deprecate Supabase Edge Function `wallets-query-snapshot`

## Repository layout

```
gsa-workers/
├── workers/
│   └── wallet_nonce_balance_daily/
│       ├── job.py
│       ├── pyproject.toml
│       └── src/
└── .github/workflows/
    └── wallet-nonce-balance-daily.yml
```
