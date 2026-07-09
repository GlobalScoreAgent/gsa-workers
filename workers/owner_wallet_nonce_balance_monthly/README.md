# Owner wallet nonce balance monthly

Batch job that queries native balance and nonce across 8 EVM chains for owner wallets in `erc_8004.wallets`, then persists JSON results to Supabase.

## Eligibility (`import_nonce_and_balance_monthly_next_eligible_at`)

The worker claims wallets when:

```sql
is_valid_import_current_nonce_and_balance_monthly IS TRUE
AND import_nonce_and_balance_monthly_next_eligible_at <= NOW()
```

| Value | Meaning |
|---|---|
| `-infinity` | Never processed; eligible immediately |
| `<= NOW()` | Due for refresh (30-day window passed or stale Pending) |
| `> NOW()` | Not eligible (recently completed or in-flight) |
| `NULL` | Out of worker scope (`is_valid` is false) |

### Maintenance

| Event | Who updates `next_eligible_at` |
|---|---|
| New owner wallet (`is_valid` becomes true) | DB trigger `trg_wallet_monthly_next_eligible_at` → `-infinity` |
| Claim (worker) | `NOW() + CLAIM_STALE_SECONDS` (default 2h) |
| Save Completed/Error (worker) | `NOW() + 30 days` |
| Downstream `Processed` | Unchanged (already scheduled at save) |

Legacy columns (`import_nonce_and_balance_monthly_at`, `import_nonce_and_balance_monthly_last_status`, JSON) remain for auditing and downstream jobs.

## Manual re-queue

Force wallets back into the claim queue:

```sql
UPDATE erc_8004.wallets
SET import_nonce_and_balance_monthly_next_eligible_at = '-infinity'
WHERE is_valid_import_current_nonce_and_balance_monthly IS TRUE
  AND import_nonce_and_balance_monthly_last_status = 'Error';
```

## Monitoring

Eligible count (fast, uses partial index):

```sql
SELECT COUNT(*) AS eligible_now
FROM erc_8004.wallets
WHERE is_valid_import_current_nonce_and_balance_monthly IS TRUE
  AND import_nonce_and_balance_monthly_next_eligible_at <= NOW();
```

Claim plan check:

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT w.id
FROM erc_8004.wallets w
WHERE w.is_valid_import_current_nonce_and_balance_monthly IS TRUE
  AND w.import_nonce_and_balance_monthly_next_eligible_at <= NOW()
ORDER BY w.import_nonce_and_balance_monthly_next_eligible_at, w.id
LIMIT 200;
```

## Environment

| Variable | Default | Description |
|---|---|---|
| `SUPABASE_DB_URL` | required | Postgres connection string |
| `ALCHEMY_KEY` | optional | Alchemy fallback after public RPCs |
| `CONCURRENCY` | 20 | Parallel wallets (max 20) |
| `CLAIM_BATCH_SIZE` | 200 | Wallets per claim batch |
| `CLAIM_STALE_SECONDS` | 7200 | Re-claim delay after Pending claim |
| `MAX_RUNTIME_SECONDS` | 19800 | Internal time budget (~5.5h) |
| `SKIP_ELIGIBLE_COUNT` | 1 | Skip blocking `COUNT(*)` at startup |
