# Owner wallet nonce balance monthly

Batch job that queries native balance and nonce across 8 EVM chains for owner wallets in `erc_8004.wallets`, persists JSON results, then applies the monthly snapshot inline (`wallet_owner_details` + `Processed`).

## Pipeline

1. **Claim** — `import_nonce_and_balance_monthly_next_eligible_at <= NOW()`
2. **RPC** — balance/nonce per chain
3. **Save** — JSON + `Completed` or `Error`
4. **Snapshot** — `erc_8004.wallet_apply_monthly_snapshot(wallet_id)` → `wallet_owner_details` + `Processed`

The pg_cron job `wallet_owner_update_transactions` is deprecated; post-processing runs in the worker.

Claim, batch save, and snapshot reconnect and retry up to 3 times on Supabase connection drops (`OperationalError` / `InterfaceError`), so a transient SSL/DB close does not kill the whole run.

Retryable DB errors (timeout, connection, deadlock) are retried 3×; if they persist, the worker skips that batch and continues until the time budget.

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
| Snapshot OK (worker) | `Processed` on `import_nonce_and_balance_monthly_last_status` |
| Downstream `Processed` | No separate cron; snapshot runs inline after save |

Legacy columns (`import_nonce_and_balance_monthly_at`, `import_nonce_and_balance_monthly_last_status`, JSON) remain for auditing.

## Backfill stuck `Completed` wallets

After disabling pg_cron, replay snapshots for wallets left in `Completed`:

```sql
SELECT erc_8004.wallet_apply_monthly_snapshot(w.id)
FROM erc_8004.wallets w
WHERE w.import_nonce_and_balance_monthly_last_status = 'Completed'
  AND w.import_current_nonce_and_balance_monthly_json IS NOT NULL
  AND w.import_current_nonce_and_balance_monthly_json <> '{}'::jsonb
ORDER BY w.id
LIMIT 50;
```

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

## Validation

```sql
-- Spot-check post-run
SELECT w.id, w.import_nonce_and_balance_monthly_last_status,
       d.chain_id, d.current_nonce, d.current_balance, d.wallet_type, d.update_nonce_at
FROM erc_8004.wallets w
JOIN erc_8004.wallet_owner_details d ON d.wallet_id = w.id
WHERE w.import_nonce_and_balance_monthly_at >= NOW() - INTERVAL '24 hours'
LIMIT 20;

-- No recent Completed left hanging
SELECT COUNT(*) FROM erc_8004.wallets
WHERE import_nonce_and_balance_monthly_last_status = 'Completed'
  AND import_nonce_and_balance_monthly_at >= NOW() - INTERVAL '7 days';
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
