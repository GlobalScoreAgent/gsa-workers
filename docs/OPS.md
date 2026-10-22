# Operations

Runbook for stuck wallets, failed Actions runs, and when to change schema vs workers.

## GHA hosts

| Host | Schedules |
|------|-----------|
| [`MichBarbarian/gsa-workers`](https://github.com/MichBarbarian/gsa-workers) | Chain-heavy (daily, owner, dune, classifier, backfill, ethos reviews) |
| [`GlobalScoreAgent/gsa-workers`](https://github.com/GlobalScoreAgent/gsa-workers) | DB-light (URI resolve/reprocess, endpoint liveness, erc8257, series export, HUMI reason) |

`gh run list` / `workflow_dispatch` must target the host that owns the workflow. Map: [AGENTS.md](../AGENTS.md#gha-hosts-split-2026-09-17).

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

Adjust column names for the owner monthly / origin lanes ([SUPABASE.md](./SUPABASE.md)).

### Owner monthly: rate-limited wallets

`owner_wallet_monthly` requeues rate-limited wallets on `TRANSIENT_REQUEUE_SECONDS` (1h) instead of marking them `Error`, so a 429 burst shows up as a clock that keeps moving in one-hour steps, not as a growing error count. Symptoms and knob:

| Symptom | Action |
|---|---|
| Repeated `Transient wallet_id=` for the same wallets | Lower `ALCHEMY_MAX_INFLIGHT` (shared by both lanes); do not raise the per-lane concurrency |
| `Partial transient` on many wallets | Public RPCs for that chain are failing; check the chain's endpoint list before blaming Alchemy |
| Origin lane throughput looks low | Expected: binary search over historical blocks. Do not raise `ORIGIN_CONCURRENCY` above 5 without measuring |

## Interpreting worker logs

### Claim workers

| Log line | Meaning |
|---|---|
| `Claimed batch size=...` | Claim OK; RPC about to run. `owner_wallet_monthly` prefixes its lane: `Claimed batch lane=monthly size=...` |
| `Reconnecting to Postgres after connection failure` | Transient SSL/DB drop; retry in progress |
| `Claim failed; will retry next loop` | Claim exhausted retries; loop continues |
| `Save/snapshot failed for batch; wallets stay Pending` | Batch not persisted; reclaim after stale window |
| `Snapshot failed for wallet id=...` | That wallet marked Error (or retried); others continue |
| `Time budget reached` | Soft stop (`MAX_RUNTIME_SECONDS`); exit 0 |
| `Critical job failure` | Unexpected error outside the DB continue path |

### Ethos reviews API

| Log line | Meaning |
|---|---|
| `Claimed batch size=` | Claim OK; Ethos activities about to run |
| `Done profile_id=` | Upsert + complete for that profile |
| `queue empty` | No GSA-linked Claimed profiles due; exit 0 |
| `Time budget reached` | Soft stop; remaining stay claimed until stale 2h |

Schema RPCs: `ethos.claim_reviews_fetch` / `complete_reviews_fetch`. Do not confuse with `ethos_scores` (credibility, TTL 15d) inside `on_demand_backfill`.

### Dune queries import

| Log line | Meaning |
|---|---|
| `=== Task N/4: … ===` | Starting one of cex / mixers / bridges / ofac |
| `Fetching Dune query … page N` | HTTP page fetch in progress |
| `Waiting … before next Dune page` | Rate-limit pacing between pages |
| `Waiting … before next task` | Rate-limit pacing between tasks |
| `Dune HTTP 429 rate limited; sleeping` | Hit Free/Plus rpm cap; retrying with backoff |
| `Fetched N rows for …; upserting in chunks` | Dune OK; chunked upsert starting |
| `Task … upsert chunk i/j` | One RPC chunk committed |
| `Task … OK` | That task succeeded |
| `Task … failed` | Task error; other tasks still run |
| `Finished … with failures` | Exit 1; at least one task failed |

## Dune queries import

Reference-data job (`dune_queries_import`). No claim / `Pending` / `next_eligible_at`. Four Dune queries per run (paginated fetch + chunked upsert). Schedule: day **18** 00:00 UTC (`0 0 18 * *`) after typical Dune billing reset ~17th (+ `workflow_dispatch`).

| Symptom | Likely cause | Action |
|---|---|---|
| Workflow fails immediately | Missing/invalid `DUNE_KEY` or `SUPABASE_DB_URL` | Check repo secrets; re-run |
| Task fails with HTTP **402** / datapoint limit | Billing-cycle quota exhausted (often `cex`) | Wait for reset (~17th) or raise limit; re-run with `dune_tasks=cex` when quota allows |
| Task fails with 0 rows | Empty/stale Dune result for that query | Check the query on Dune; re-run when data exists |
| Upsert / function does not exist | Migration not applied | Deploy `wallets_*_upsert` + tables in **gsa-supabase-schema** first |
| Upsert timeout | Large chunk / DB load | Lower `UPSERT_CHUNK_SIZE`; check `statement_timeout` (worker uses 600s) |
| One task failed, others OK | Partial run | Fix that query/RPC; re-run workflow (upserts are idempotent) |
| `max(updated_at)` old | Schedule not run or last run failed | `workflow_dispatch` on **Dune queries import** |

**Re-run:** GitHub → **Actions** → **Dune queries import** → **Run workflow**.

**Verify after a successful run** (CEX ~36k for query `7520736`):

```sql
SELECT 'cex' AS src, count(*) AS rows, max(updated_at) AS last_updated FROM wallets.cex_addresses
UNION ALL
SELECT 'mixers', count(*), max(updated_at) FROM wallets.mixer_addresses
UNION ALL
SELECT 'bridges', count(*), max(updated_at) FROM wallets.bridge_addresses
UNION ALL
SELECT 'ofac', count(*), max(updated_at) FROM wallets.ofac_sanction_addresses;
```

More monitoring SQL: [SUPABASE.md](./SUPABASE.md). Worker details: [dune_queries_import/README.md](../workers/dune_queries_import/README.md).

## Agent URI resolve / reprocess

URI workers claim agents / feedbacks / `agent_manifest` / `uri_documents` (not wallet `Pending` status). Soft `MAX_RUNTIME_SECONDS=19800`. Optional secrets: `PINATA_GATEWAY` (dedicated gateway key → last IPFS fallback after public gateways; host `violet-efficient-aardwolf-922.mypinata.cloud`), `SCRAPING_ANT_KEY`.

### Interpret logs

| Log pattern | Meaning |
|---|---|
| `Claimed agents batch size=` | Resolve claimed agents |
| `Claimed on-chain feedbacks batch size=` | On-chain DB materialize |
| `Claimed feedbacks batch size=` | External URI/endpoint feedbacks |
| `Claimed error manifests batch size=` | Reprocess download-error queue |
| `Claimed refresh docs batch size=` | Reprocess off-chain &gt;15d queue |
| `Refresh unchanged doc_id=` | Document same; TTL renewed only |
| `stamped fetched_at` / `refresh_error:` | Refresh fetch failed; previous JSON kept; `fetched_at` advanced so the row leaves the current 15d window |
| `Time budget reached` | Soft stop; exit 0; next cron continues |
| Playwright / scrape / download failures | Recorded as download error on manifest; `agent_uri_reprocess` retries (max 3) |

### Symptoms

| Symptom | Likely cause | Action |
|---|---|---|
| `agents_pending` stuck high | Resolve not running / claim index miss | Check GHA `agent-uri-resolve` schedule; confirm `is_uri_processed = false` + indexes in schema |
| Manifests with `has_download_error` forever | Exhausted `reprocess_count` (≥3) or not due yet | Wait 3d between retries; or set `does_need_manual_reprocess`; inspect URI from agents/feedbacks |
| Off-chain docs never refresh | Wrong schedule or not HTTP/IPFS | Reprocess only at 06/18; hex/on-chain excluded by design |
| Same `doc_id` claimed all run; `refresh=` high but queue stuck | Failed fetch did not stamp `fetched_at` (fixed: stamp on fail) | Confirm worker ≥ stamp-on-fail commit; look for `refresh_error:` on `source_gateway` |
| Duplicate URI content across rows | Legacy pre-`uri_hash` data | Schema migration `00066` path; upsert is by `uri_hash` |

**Re-run:** Actions → **agent-uri-resolve** or **agent-uri-reprocess** → **Run workflow**.

Monitoring SQL: [SUPABASE.md](./SUPABASE.md) (Agent URI sections). READMEs: [`agent_uri_resolve`](../workers/agent_uri_resolve/README.md), [`agent_uri_reprocess`](../workers/agent_uri_reprocess/README.md).

## Dual daily workers

`wallet_nonce_balance_daily` runs **two** GHA jobs (`worker-a`, `worker-b`) with separate concurrency groups. They share the same claim SQL (`FOR UPDATE SKIP LOCKED`), so batches do not overlap. Both need the same secrets.

Do not lower `CLAIM_STALE_SECONDS` too far: a slow RPC batch must finish before another runner reclaims the same wallets.

## Alchemy / RPC

- Public RPCs first (`networks.py`); Alchemy is fallback (`ALCHEMY_KEY` + `erc_8004.chains.subdomain_alchemy`).
- HTTP 500s from a public endpoint are normal; the client tries the next URL / Alchemy.
- If Alchemy is missing, expect more `Error` wallets on flaky public RPCs.

## Scheduled runs fire hours late

**Account-wide, not worker-specific.** Measured 2026-09-16 on `MichBarbarian/gsa-workers`:

| Workflow | Cron slot | Actual start (UTC) | Drift |
|---|---|---|---|
| `humi-reason-publisher` | 06:00 | 10:29 | +4h29 |
| `humi-reason-publisher` | 12:00 | 15:46 | +3h46 |
| `ethos-reviews-api` | 06:00 | 10:38 | +4h38 |
| `ai-agent-classifier` | 06:00 | 11:03 | +5h03 |
| `agent-endpoint-liveness` | 06:00 | 10:51 | +4h51 |

Every worker is on `0 0,6,12,18` UTC, so ~18 workflows ask for runners at the same minute four times a day and GitHub releases them as capacity frees up. Nothing is broken and nothing is lost — each run still drains its queue — but **any freshness guarantee stated in cron terms is wrong by several hours**. A "worst case 6 h stale" claim is really closer to 10 h.

Consequences worth keeping in mind:

- Do not diagnose a "missing" run before checking `run_started_at`; it is usually late, not skipped.
- If this starts to hurt, the fix is staggering the crons across the hour rather than adding retries. Not done yet.

```powershell
# Drift check for one workflow
$r = gh api "repos/MichBarbarian/gsa-workers/actions/workflows/<name>.yml/runs?per_page=10" | ConvertFrom-Json
$r.workflow_runs | Where-Object {$_.event -eq "schedule"} | Select-Object run_started_at,conclusion
```

## Re-run a job

GitHub → **Actions** → workflow name → **Run workflow** (`workflow_dispatch`).

No need to change code for a plain re-run. After a schema change, deploy the migration in **gsa-supabase-schema** first.

## Schema vs worker

| Touch | Repo |
|---|---|
| Snapshot / upsert RPCs (`wallet_apply_*`, Dune reference tables), URI indexes/helpers, triggers, new columns | `gsa-supabase-schema` |
| Claim/save/retry/job loop/GHA env/RPC / Dune / URI resolve clients | `gsa-workers` |

Deploy order when both change: **schema → worker → workflow_dispatch**.

## Related

- [PROCESSES.md](./PROCESSES.md) — live pipeline catalog (#13 on-demand, **#16 Ethos reviews API**, **#17 HUMI reason publisher**)
- [SUPABASE.md](./SUPABASE.md) — monitoring and backfill SQL
- [ARCHITECTURE.md](./ARCHITECTURE.md) — pipeline and budgets
- [DEPRECATION.md](./DEPRECATION.md) — do not re-enable old crons / Edge URI
