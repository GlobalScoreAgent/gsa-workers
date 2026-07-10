# Owner wallet origin

Batch job that finds the first on-chain activity block/date per chain (binary search on historical RPC) for owner wallets, then persists results and applies the owner history snapshot inline.

## Eligibility (`import_wallet_history_next_eligible_at`)

```sql
is_valid_import_current_nonce_and_balance_monthly IS TRUE
AND import_wallet_history_next_eligible_at <= NOW()
```

| Value | Meaning |
|---|---|
| `-infinity` | Never processed; eligible immediately |
| `<= NOW()` | 30-day window passed or stale Pending |
| `> NOW()` | Recently completed or in-flight |
| `NULL` | Out of scope (`is_valid_monthly` false) |

### Maintenance

| Event | Who updates `next_eligible_at` |
|---|---|
| Flag monthly becomes true | Trigger `trg_wallet_history_next_eligible_at` → `-infinity` |
| Claim | `NOW() + CLAIM_STALE_SECONDS` |
| Save Completed/Error | `NOW() + 30 days` |
| Snapshot success | Status → `Processed` |

## Pipeline

1. Claim via partial index on `next_eligible_at`
2. Binary-search origin per chain (shared HTTP client; public RPCs first, Alchemy backup)
3. Batch save JSON with status `Completed` or `Error`
4. `erc_8004.wallet_apply_owner_history_snapshot(wallet_id)` for each `Completed` wallet
5. Snapshot upserts `wallet_owner_details.first_transaction_at` and sets `Processed`

The pg_cron job `wallet_owner_update_first_transactions` is deprecated.

## Manual re-queue

```sql
UPDATE erc_8004.wallets
SET import_wallet_history_next_eligible_at = '-infinity'
WHERE is_valid_import_current_nonce_and_balance_monthly IS TRUE
  AND import_wallet_history_status = 'Error';
```

## Backfill stuck `Completed` wallets (pre-deploy)

```sql
SELECT erc_8004.wallet_apply_owner_history_snapshot(w.id)
FROM erc_8004.wallets w
WHERE w.import_wallet_history_status = 'Completed'
  AND w.import_wallet_history_data IS NOT NULL
  AND w.import_wallet_history_data <> '{}'::jsonb
ORDER BY w.id
LIMIT 50;
```

## Monitoring

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT w.id
FROM erc_8004.wallets w
WHERE w.is_valid_import_current_nonce_and_balance_monthly IS TRUE
  AND w.import_wallet_history_next_eligible_at <= NOW()
ORDER BY w.import_wallet_history_next_eligible_at, w.id
LIMIT 50;
```

## Environment

| Variable | Default | Description |
|---|---|---|
| `SUPABASE_DB_URL` | required | Postgres connection string |
| `ALCHEMY_KEY` | optional | Alchemy archival fallback |
| `CONCURRENCY` | 4 | Parallel wallets (max 5) |
| `CLAIM_BATCH_SIZE` | 50 | Wallets per claim batch |
| `CLAIM_STALE_SECONDS` | 7200 | Re-claim delay after Pending |
| `MAX_RUNTIME_SECONDS` | 19800 | Internal time budget (~5.5h) |
| `SKIP_ELIGIBLE_COUNT` | 1 | Skip blocking COUNT at startup |
