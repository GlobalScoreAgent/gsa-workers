# GSA Workers

Unified Python batch workers for [Global Score Agent](https://www.globalscoreagent.com/), run via GitHub Actions.

## Workers

| Worker | Schedule | Description |
|---|---|---|
| `wallet_nonce_balance_daily` | 4×/day UTC (0, 6, 12, 18h) | Balance + nonce across 8 EVM chains → `erc_8004.wallets` |
| `owner_wallet_origin` | 4×/day UTC (0, 6, 12, 18h) | Wallet origin/history across 8 chains, refresh every 30 days → `import_wallet_history_data` |
| `owner_wallet_nonce_balance_monthly` | 4×/day UTC (0, 6, 12, 18h) | Balance + nonce monthly refresh (30-day window) → `import_current_nonce_and_balance_monthly_json` |

## owner_wallet_nonce_balance_monthly

Monthly balance and nonce snapshot across 8 EVM chains for wallets flagged for monthly import.

**Eligible wallets:**

- `is_valid_import_current_nonce_and_balance_monthly = true`
- `import_nonce_and_balance_monthly_at` IS NULL or older than 30 days
- `import_nonce_and_balance_monthly_last_status` IS NULL, `Completed`, `Error`, `Processed`, or stale `Pending`

**Writes:**

- `import_current_nonce_and_balance_monthly_json` (jsonb with per-chain balance/nonce)
- `import_nonce_and_balance_monthly_last_status` (`Pending` → `Completed` | `Error`)
- `import_nonce_and_balance_monthly_at` (set on completion)

**Auto-shutdown:** exits immediately if no eligible wallets at start, or when the queue is drained during a run. Schedule stays enabled for continuous daily processing.

Wallets with `Error` or legacy `Processed` are re-eligible after 30 days (same as `Completed`).

### Local development

```powershell
cd workers/owner_wallet_nonce_balance_monthly
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
| `CONCURRENCY` | No | `15` (max 20) |
| `CLAIM_BATCH_SIZE` | No | `100` |
| `CLAIM_STALE_SECONDS` | No | `7200` (2h stale Pending reclaim) |
| `MAX_RUNTIME_SECONDS` | No | `19800` (5.5h) |

Manual run: **Actions** → **Owner wallet nonce balance monthly** → **Run workflow**.

## owner_wallet_origin

Wallet activation block/date per chain (binary search on historical RPC), refreshed on a 30-day cycle.

**Eligible wallets:**

- `is_valid_import_current_nonce_and_balance_monthly = true`
- `import_wallet_history_at` IS NULL or older than 30 days
- `import_wallet_history_status` IS NULL, `Completed`, `Error`, `Processed`, or stale `Pending`

**Writes:**

- `import_wallet_history_data` (jsonb with per-chain results)
- `import_wallet_history_status` (`Pending` → `Completed` | `Error`)
- `import_wallet_history_at` (set on completion)

**Auto-shutdown:** exits immediately if no eligible wallets at start, or when the queue is drained during a run. The daily schedule stays enabled so new eligible wallets are picked up on the next cron slot.

Wallets with `Error` or legacy `Processed` are re-eligible after 30 days (same as `Completed`).

### Local development

```powershell
cd workers/owner_wallet_origin
copy .env.example .env
# Edit .env with SUPABASE_DB_URL and ALCHEMY_KEY

uv sync
uv run python job.py
uv run python scripts/check_pending.py
```

### GitHub Actions secrets

| Secret / Var | Required | Default |
|---|---|---|
| `SUPABASE_DB_URL` | Yes | Postgres connection string (pooler) |
| `ALCHEMY_KEY` | Recommended | Alchemy archival fallback |
| `CONCURRENCY` | No | `4` (max 5) |
| `CLAIM_BATCH_SIZE` | No | `25` |
| `CLAIM_STALE_SECONDS` | No | `7200` (2h stale Pending reclaim) |
| `MAX_RUNTIME_SECONDS` | No | `19800` (5.5h) |

Manual run: **Actions** → **Owner wallet origin** → **Run workflow**.

## wallet_nonce_balance_daily

Two parallel GitHub Actions runners (`worker-a`, `worker-b`) claim disjoint wallet batches via atomic `Pending` locks in Postgres.

Reads claimable wallets from `erc_8004.wallets`:

- `is_valid_import_current_nonce_and_balance_daily = true`
- `import_nonce_and_balance_daily_at` is NULL or before today (UTC)
- `import_nonce_and_balance_daily_last_status` IS NULL, `Completed`, `Error`, `Processed`, or stale `Pending`
- legacy `Processed` and `Error` are re-eligible on the next UTC day (same as `Completed`)

Writes:

- `import_current_nonce_and_balance_daily_json`
- `import_nonce_and_balance_daily_last_status` (`Pending` → `Completed` | `Error`)
- `import_nonce_and_balance_daily_at` (set only on completion)
- `import_nonce_and_balance_daily_claimed_at` / `claimed_by` (cleared on completion)

Stale `Pending` claims older than `CLAIM_STALE_SECONDS` (default 2h) are automatically reclaimed.

RPC strategy per chain:

1. Public RPC fallbacks (`networks.py`)
2. Alchemy JSON-RPC batch fallback (`alchemy.py`, pattern from `wallet-transactional-current-batch`)
3. `subdomain_alchemy` loaded from `erc_8004.chains`

Wallets with `Error` or `Processed` are **not** retried the same UTC day; they become eligible again the next UTC day.

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
| `WORKER_ID` | No | `worker-a` (set per matrix job in CI) |
| `CONCURRENCY` | No | `15` (max 20) |
| `CLAIM_BATCH_SIZE` | No | `100` |
| `CLAIM_STALE_SECONDS` | No | `7200` (2h stale Pending reclaim) |
| `MAX_RUNTIME_SECONDS` | No | `19800` (5.5h) |

Manual run: **Actions** → **Wallet nonce balance daily** → **Run workflow** (starts both `worker-a` and `worker-b`).

### Phase 2 (deprecation)

After this worker is stable in production:

- Deprecate Cloudflare Worker `wallet-snapshot` (`gsa-cloudflare-workers`)
- Deprecate Supabase Edge Function `wallets-query-snapshot`

## Repository layout

```
gsa-workers/
├── workers/
│   ├── wallet_nonce_balance_daily/
│   │   ├── job.py
│   │   ├── pyproject.toml
│   │   └── src/
│   └── owner_wallet_origin/
│       ├── job.py
│       ├── scripts/
│       │   ├── check_pending.py
│       │   └── compare_smoke.py
│       ├── pyproject.toml
│       └── src/
│   └── owner_wallet_nonce_balance_monthly/
│       ├── job.py
│       ├── pyproject.toml
│       └── src/
└── .github/workflows/
    ├── wallet-nonce-balance-daily.yml
    ├── owner-wallet-origin.yml
    └── owner-wallet-nonce-balance-monthly.yml
```
