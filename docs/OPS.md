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

### Claim workers

| Log line | Meaning |
|---|---|
| `Claimed batch size=...` | Claim OK; RPC about to run |
| `Reconnecting to Postgres after connection failure` | Transient SSL/DB drop; retry in progress |
| `Claim failed; will retry next loop` | Claim exhausted retries; loop continues |
| `Save/snapshot failed for batch; wallets stay Pending` | Batch not persisted; reclaim after stale window |
| `Snapshot failed for wallet id=...` | That wallet marked Error (or retried); others continue |
| `Time budget reached` | Soft stop (`MAX_RUNTIME_SECONDS`); exit 0 |
| `Critical job failure` | Unexpected error outside the DB continue path |

### CEX addresses import

| Log line | Meaning |
|---|---|
| `Fetching Dune query … page N` | HTTP page fetch in progress |
| `Waiting … before next Dune page` | Rate-limit pacing between pages |
| `Dune HTTP 429 rate limited; sleeping` | Hit Free/Plus rpm cap; retrying with backoff |
| `Fetched N rows from Dune; calling wallets.cex_addresses_upsert` | Dune OK; about to upsert |
| `Done in …s — WALLETS CEX ADDRESSES UPSERT → N rows` | Success |
| `Dune returned 0 rows; refusing to upsert` | Exit 1; table left unchanged |
| `Dune fetch failed` / `Upsert failed` | Exit 1; fix secret/RPC/network and re-run |

## CEX addresses import

Reference-data job (`cex_addresses_import`). No claim / `Pending` / `next_eligible_at`.

| Symptom | Likely cause | Action |
|---|---|---|
| Workflow fails immediately | Missing/invalid `DUNE_KEY` or `SUPABASE_DB_URL` | Check repo secrets; re-run |
| `Dune returned 0 rows` | Empty/stale Dune result | Check query `7520736` on Dune; re-run when data exists |
| Upsert / function does not exist | Migration not applied | Deploy `wallets.cex_addresses_upsert` in **gsa-supabase-schema** first |
| Upsert timeout | Large payload / DB load | Check `statement_timeout`; re-run; chunk only if pooler rejects payload |
| `max(updated_at)` old | Schedule not run or last run failed | `workflow_dispatch` on **CEX addresses import** |

**Re-run:** GitHub → **Actions** → **CEX addresses import** → **Run workflow**.

**Verify after a successful run** (~36k rows expected for query `7520736`):

```sql
SELECT count(*) AS rows, max(updated_at) AS last_updated
FROM wallets.cex_addresses;
```

More monitoring SQL: [SUPABASE.md](./SUPABASE.md). Worker details: [cex_addresses_import/README.md](../workers/cex_addresses_import/README.md).

## Token prices import

Reference-data job (`token_prices_import`). No claim / `Pending` / `next_eligible_at`.

| Symptom | Likely cause | Action |
|---|---|---|
| Workflow fails immediately | Missing/invalid `DUNE_KEY` or `SUPABASE_DB_URL` | Check repo secrets; re-run |
| `Dune returned 0 rows` | Empty/stale Dune result | Check query `7526826` on Dune; re-run when data exists |
| `Dune HTTP 429` after retries | Rate limit (Free high-limit ≈ 40 rpm) | Wait a minute; keep `DUNE_PAGE_DELAY_SECONDS=2`; Plus can lower to `0.3` |
| Upsert / function does not exist | Migration not applied | Deploy `wallets.token_prices_upsert` in **gsa-supabase-schema** first |
| Upsert timeout | Large payload / DB load | Lower `UPSERT_CHUNK_SIZE` (default 5000); check `statement_timeout` |
| `max(price_date)` old | Dune query stale or last run failed | Fix Dune query; `workflow_dispatch` on **Token prices import** |

**Re-run:** GitHub → **Actions** → **Token prices import** → **Run workflow**.

**Verify after a successful run** (~2k–225k rows per Dune latest result depending on query filters; conflicts are no-ops):

```sql
SELECT count(*) AS rows, max(price_date) AS max_price_date
FROM wallets.token_prices;
```

Large imports use `DUNE_PAGE_DELAY_SECONDS=2` (Free ≈ 40 rpm) and `UPSERT_CHUNK_SIZE=5000`. Worker details: [token_prices_import/README.md](../workers/token_prices_import/README.md).

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
| Snapshot function body, `wallets.cex_addresses_upsert`, triggers, indexes, new columns | `gsa-supabase-schema` |
| Claim/save/retry/job loop/GHA env/RPC clients / Dune client | `gsa-workers` |

Deploy order when both change: **schema → worker → workflow_dispatch**.

## Related

- [SUPABASE.md](./SUPABASE.md) — monitoring and backfill SQL
- [ARCHITECTURE.md](./ARCHITECTURE.md) — pipeline and budgets
- [DEPRECATION.md](./DEPRECATION.md) — do not re-enable old crons
