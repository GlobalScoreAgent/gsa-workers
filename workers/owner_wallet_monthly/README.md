# Owner wallet monthly (monthly + origin lanes)

> Project context: [AGENTS.md](../../AGENTS.md) · [Supabase map](../../docs/SUPABASE.md) · [Architecture](../../docs/ARCHITECTURE.md)

Single batch job that covers the two owner-wallet tasks that used to live in
`owner_wallet_nonce_balance_monthly` and `owner_wallet_origin` (both deleted
2026-09-16, see [DEPRECATION.md](../../docs/DEPRECATION.md)):

| Lane | Clock | Payload | Snapshot | Destination |
|---|---|---|---|---|
| `monthly` | `import_nonce_and_balance_monthly_next_eligible_at` | `import_current_nonce_and_balance_monthly_json` | `wallet_apply_monthly_snapshot` | `wallet_owner_details` current metrics |
| `origin` | `import_wallet_history_next_eligible_at` | `import_wallet_history_data` | `wallet_apply_owner_history_snapshot` | `wallet_owner_details.first_transaction_at` |

Both lanes share the eligibility gate `is_valid_import_current_nonce_and_balance_monthly`,
the 30-day cadence and the `ALCHEMY_KEY` secret, which is why they were merged.
The data model did not change: two clocks, two payload columns, two status columns,
two snapshots.

## Two lanes, one process

The lanes run as two concurrent asyncio tasks. They share the Postgres connection
(serialized by a lock), the HTTP client and the Alchemy inflight budget; they do not
share claims, clocks, batch sizes or concurrency.

Running them in parallel rather than one after the other is deliberate: `origin` does a
binary search over historical blocks and has taken **131 minutes** in a single run,
while `monthly` reads `latest` and finishes in 5–20 minutes. Sequential lanes would
leave `monthly` starved behind a heavy `origin` batch inside the same
`MAX_RUNTIME_SECONDS` budget.

```
lane monthly: claim → balance+nonce at latest → save → wallet_apply_monthly_snapshot
lane origin:  claim → binary search first activity → save → wallet_apply_owner_history_snapshot
```

## Eligibility

```sql
-- monthly lane
is_valid_import_current_nonce_and_balance_monthly IS TRUE
AND import_nonce_and_balance_monthly_next_eligible_at <= NOW()

-- origin lane
is_valid_import_current_nonce_and_balance_monthly IS TRUE
AND import_wallet_history_next_eligible_at <= NOW()
```

| Value | Meaning |
|---|---|
| `-infinity` | Never processed; eligible immediately |
| `<= NOW()` | Due (30-day window passed or stale Pending) |
| `> NOW()` | Recently completed or in-flight |
| `NULL` | Out of scope (`is_valid` false) |

A lane only ever touches its own clock. A wallet whose `monthly` clock is due but whose
`origin` clock is not gets claimed by the monthly lane alone, and
`import_wallet_history_next_eligible_at` is left untouched.

| Event | Who updates `next_eligible_at` |
|---|---|
| `is_valid` becomes true | Triggers `trg_wallet_monthly_next_eligible_at` / `trg_wallet_history_next_eligible_at` → `-infinity` |
| Claim | `NOW() + CLAIM_STALE_SECONDS` (default 2h) |
| Save Completed/Error | `NOW() + 30 days` |
| Rate-limited chain | `NOW() + TRANSIENT_REQUEUE_SECONDS` (default 1h) |

## Rate limits are not errors

`ALCHEMY_KEY` is shared with `wallet_nonce_balance_daily`, so bursts of HTTP 429 are
expected. Public endpoints are tried first and a failure there just moves to the next
endpoint; Alchemy is the last resort and goes through
[`src/backoff.py`](./src/backoff.py), which retries 429 / 5xx / timeouts honouring
`Retry-After` with exponential backoff, capped at 32s.

If a chain still fails on rate limit after the retries, it is reported with
`status="transient"` and the wallet is handled like this:

| Case | Payload | Status | Clock |
|---|---|---|---|
| Some chains OK, some transient | saved (partial) | `Completed` + snapshot | `NOW() + TRANSIENT_REQUEUE_SECONDS` |
| No chain succeeded and at least one was transient | not written | untouched (stays `Pending` from the claim) | `NOW() + TRANSIENT_REQUEUE_SECONDS` |
| Real failure (bad address, RPC error) | saved | `Error` | `NOW() + 30 days` |

A 429 never produces status `Error`. This is the lesson from the discovery pipeline,
where 429 marked as permanent errors silently froze ~17 500 rows for two weeks
(ADR 2026-09-16, unify token/LP discovery).

`ALCHEMY_MAX_INFLIGHT` caps concurrent Alchemy calls across **both** lanes, so the
worker holds one coordinated budget instead of two workflows competing on the same key.

## Manual re-queue

```sql
-- monthly lane
UPDATE erc_8004.wallets
SET import_nonce_and_balance_monthly_next_eligible_at = '-infinity'
WHERE is_valid_import_current_nonce_and_balance_monthly IS TRUE
  AND import_nonce_and_balance_monthly_last_status = 'Error';

-- origin lane
UPDATE erc_8004.wallets
SET import_wallet_history_next_eligible_at = '-infinity'
WHERE is_valid_import_current_nonce_and_balance_monthly IS TRUE
  AND import_wallet_history_status = 'Error';
```

## Monitoring

```sql
SELECT
  count(*) FILTER (WHERE import_nonce_and_balance_monthly_next_eligible_at <= NOW()) AS monthly_due,
  count(*) FILTER (WHERE import_wallet_history_next_eligible_at <= NOW()) AS origin_due,
  count(*) FILTER (WHERE import_nonce_and_balance_monthly_last_status = 'Error') AS monthly_error,
  count(*) FILTER (WHERE import_wallet_history_status = 'Error') AS origin_error,
  count(*) FILTER (WHERE import_nonce_and_balance_monthly_last_status = 'Pending') AS monthly_pending,
  count(*) FILTER (WHERE import_wallet_history_status = 'Pending') AS origin_pending
FROM erc_8004.wallets
WHERE is_valid_import_current_nonce_and_balance_monthly IS TRUE;
```

Both claims hit partial indexes (`idx_wallets_nonce_balance_monthly_next_eligible`,
`idx_wallets_wallet_history_next_eligible`).

Post-run spot check:

```sql
SELECT w.id, w.import_nonce_and_balance_monthly_last_status, w.import_wallet_history_status,
       d.chain_id, d.current_nonce, d.current_balance, d.first_transaction_at
FROM erc_8004.wallets w
JOIN erc_8004.wallet_owner_details d ON d.wallet_id = w.id
WHERE w.import_nonce_and_balance_monthly_at >= NOW() - INTERVAL '24 hours'
LIMIT 20;
```

Queue check from the repo: `uv run python scripts/check_pending.py`. Public-RPC smoke
test for both lanes: `uv run python scripts/compare_smoke.py`.

## Backfill stuck `Completed` wallets

```sql
SELECT erc_8004.wallet_apply_monthly_snapshot(w.id)
FROM erc_8004.wallets w
WHERE w.import_nonce_and_balance_monthly_last_status = 'Completed'
  AND w.import_current_nonce_and_balance_monthly_json IS NOT NULL
  AND w.import_current_nonce_and_balance_monthly_json <> '{}'::jsonb
ORDER BY w.id
LIMIT 50;

SELECT erc_8004.wallet_apply_owner_history_snapshot(w.id)
FROM erc_8004.wallets w
WHERE w.import_wallet_history_status = 'Completed'
  AND w.import_wallet_history_data IS NOT NULL
  AND w.import_wallet_history_data <> '{}'::jsonb
ORDER BY w.id
LIMIT 50;
```

## Logs

| Line | Meaning |
|---|---|
| `Claimed batch lane=monthly size=…` | Lane claimed work |
| `Transient wallet_id=… lane=…` | Every chain rate-limited; wallet requeued short, no payload |
| `Partial transient wallet_id=… lane=…` | Partial data saved, clock shortened |
| `Finished monthly_processed=… origin_processed=…` | Run summary per lane |

## Environment

| Variable | Default | Description |
|---|---|---|
| `SUPABASE_DB_URL` | required | Postgres connection string |
| `ALCHEMY_KEY` | optional | Alchemy fallback after public RPCs |
| `MONTHLY_CONCURRENCY` | 15 (CI 20) | Parallel wallets in the monthly lane (max 20) |
| `MONTHLY_CLAIM_BATCH_SIZE` | 100 (CI 200) | Wallets per monthly claim |
| `ORIGIN_CONCURRENCY` | 4 | Parallel wallets in the origin lane (max 5) |
| `ORIGIN_CLAIM_BATCH_SIZE` | 50 | Wallets per origin claim |
| `ALCHEMY_MAX_INFLIGHT` | 8 | Concurrent Alchemy calls shared by both lanes |
| `CLAIM_STALE_SECONDS` | 7200 | Re-claim delay after Pending |
| `TRANSIENT_REQUEUE_SECONDS` | 3600 | Short requeue after a rate limit |
| `MAX_RUNTIME_SECONDS` | 19800 | Time budget shared by both lanes (~5.5h) |
| `SKIP_ELIGIBLE_COUNT` | 1 | Skip the blocking `COUNT(*)` at startup |
