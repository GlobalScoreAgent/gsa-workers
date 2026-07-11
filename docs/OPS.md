# Operations

Runbook for stuck wallets, failed Actions runs, and when to change schema vs workers.

## Stuck states

| Symptom | Likely cause | Action |
|---|---|---|
| Many `Pending`, `next_eligible_at` in the future | Claimed then job died before save | Wait until `CLAIM_STALE_SECONDS` (2h) or force re-queue (below) |
| `Completed` with non-empty JSON, not `Processed` | Snapshot never ran (old cron off / worker crash mid-batch) | Backfill `wallet_apply_*_snapshot` in batches of 50 — see [SUPABASE.md](./SUPABASE.md) |
| `Error` | RPC all-chains failed or snapshot marked error | Fix root cause; re-queue with `next_eligible_at = '-infinity'` |
| Claim timeouts in logs | Heavy DB load / missing index | Check `EXPLAIN` on claim; confirm partial index on `next_eligible_at` in schema repo |

### Force re-queue (example: daily Errors)

```sql
UPDATE erc_8004.wallets
SET import_nonce_and_balance_daily_next_eligible_at = '-infinity'
WHERE is_valid_import_current_nonce_and_balance_daily IS TRUE
  AND import_nonce_and_balance_daily_last_status = 'Error';
```

Adjust column names for monthly / origin ([SUPABASE.md](./SUPABASE.md)).

## Interpreting worker logs

| Log line | Meaning |
|---|---|
| `Claimed batch size=...` | Claim OK; RPC about to run |
| `Reconnecting to Postgres after connection failure` | Transient SSL/DB drop; retry in progress |
| `Claim failed; will retry next loop` | Claim exhausted retries; loop continues |
| `Save/snapshot failed for batch; wallets stay Pending` | Batch not persisted; reclaim after stale window |
| `Snapshot failed for wallet id=...` | That wallet marked Error (or retried); others continue |
| `Time budget reached` | Soft stop (`MAX_RUNTIME_SECONDS`); exit 0 |
| `Critical job failure` | Unexpected error outside the DB continue path |

## Dual daily workers

`wallet_nonce_balance_daily` runs **two** GHA jobs (`worker-a`, `worker-b`) with separate concurrency groups. They share the same claim SQL (`FOR UPDATE SKIP LOCKED`), so batches do not overlap. Both need the same secrets.

Do not lower `CLAIM_STALE_SECONDS` too far: a slow RPC batch must finish before another runner reclaims the same wallets.

## Alchemy / RPC

- Public RPCs first (`networks.py`); Alchemy is fallback (`ALCHEMY_KEY` + `erc_8004.chains.subdomain_alchemy`).
- HTTP 500s from a public endpoint are normal; the client tries the next URL / Alchemy.
- If Alchemy is missing, expect more `Error` wallets on flaky public RPCs.

## Re-run a job

GitHub → **Actions** → workflow name → **Run workflow** (`workflow_dispatch`).

No need to change code for a plain re-run. After a schema change, deploy the migration in **gsa-supabase-schema** first.

## Schema vs worker

| Touch | Repo |
|---|---|
| Snapshot function body, triggers, indexes, new columns | `gsa-supabase-schema` |
| Claim/save/retry/job loop/GHA env/RPC clients | `gsa-workers` |

Deploy order when both change: **schema → worker → workflow_dispatch**.

## Related

- [SUPABASE.md](./SUPABASE.md) — monitoring and backfill SQL
- [ARCHITECTURE.md](./ARCHITECTURE.md) — pipeline and budgets
- [DEPRECATION.md](./DEPRECATION.md) — do not re-enable old crons
