# HUMI Reason Publisher

> Project context: [AGENTS.md](../../AGENTS.md) · [Process catalog](../../docs/PROCESSES.md) · [Supabase map](../../docs/SUPABASE.md) · [Architecture](../../docs/ARCHITECTURE.md)

**Status: live after schema deploy** (cron `0 0,6,12,18 * * *` UTC + `workflow_dispatch`).

Publishes the HUMI narrative aggregate of each agent as a JSON object in the **private** Supabase Storage bucket `humi-reasons`, so `index_humi.index_humi_agent` stops carrying ~12 kB of TOAST per row through every recalculation.

**ADR:** vault `08 - Decisiones/2026-09-15 - Toast JSON frios a Supabase Storage`  
**Schema:** `gsa-supabase-schema` → `supabase/docs/toast-cold-storage.md` (`20260916010000_humi_reason_publish_claim.sql`, `20260916010100_agent_index_humi_calculate_reason_publish_flag.sql`)  
**Vault ops:** `12 - Github Worker/HUMI Reason Publisher/`

The worker is a **renderer, not an engine**: every `*_score` keeps being computed and persisted by SQL. It only assembles the document and uploads it.

## Why a fixed cron and not "when the HUMI lane finishes"

The lane has no stable finish time. It always starts at 18:00 UTC (`job_control.start_daily_index_pipeline`) but measured in `cron.job_run_details` it took 12 minutes on quiet days (Sep 4-8) and close to 24 hours on loaded ones (Sep 9, 11, 13). Anchoring the worker to that moment would miss by up to a day.

The claim is idempotent and content-hashed, so running mid-lane is harmless: it drains whatever is flagged and the next run picks up the rest. Consequence: the narrative can lag the score by up to ~6 h, accepted silently (the UI shows the previous version, same as it already does between daily cycles).

## Pipeline

```
claim_reason_publish (SKIP LOCKED + soft-lock)
  → SELECT the 4 pillar_* rows for the batch
  → assemble the aggregate (src/pillar_spec.py drives both the SELECT and the build)
  → sha256 of the serialized document
      == stored sha  → skip upload, just clear the flag
      != stored sha  → POST /storage/v1/object/humi-reasons/humi/agent/{id}.json (x-upsert)
  → complete_reason_publish
```

Empty queue → log `queue empty` → **exit 0**. Cap `MAX_RUNTIME_SECONDS=19800`.

`complete_reason_publish` receives the `version` captured at claim time. If the lane recalculated the agent meanwhile, the flag stays `true` so the stale object gets republished next run.

### Failure handling

Failed rows **keep** their soft-lock instead of being released. Releasing them would make the next `claim_reason_publish` return the exact same rows (the claim orders by `reason_publish_claimed_at NULLS FIRST, agent_id`), so the run would spin on a poisoned batch and never advance. Holding the lock lets the run move on; the rows return to the queue on their own once `CLAIM_STALE_SECONDS` expires.

On top of that, three consecutive batches with zero successful rows abort the run with exit 1. That is the systemic-failure case (expired key, deleted bucket) and there is no point burning the 5.5 h slot on it.

To unlock rows manually before the soft-lock expires:

```sql
SELECT index_humi.release_reason_publish(ARRAY[123, 456]::bigint[]);
```

## Document

`humi-reasons/humi/agent/{agent_id}.json`, private bucket, read server-side with service role.

```json
{
  "schema": 1,
  "agent_id": 2,
  "pillar_history_summary": { "block_basic_score": 10, "block_basic_items": [ { "name": "...", "points": 5, "reason": { } } ], "...": "...", "summary": { } },
  "pillar_information_summary": { },
  "pillar_measure_summary": { },
  "pillar_usage_summary": { }
}
```

Keys use the **view-facing** names (`pillar_*_summary`, as `web_dashboard.index_humi_live` exposes them), so the web hydrates the field directly.

No timestamp inside the document on purpose: it would change the sha on every run and kill the no-op short-circuit. Freshness lives in `index_humi_agent.reason_published_at`.

## Env

| Variable | Default | Role |
|---|---|---|
| `SUPABASE_DB_URL` | required | Pooler DSN |
| `SUPABASE_URL` | required | Project URL for the Storage REST API |
| `SUPABASE_SERVICE_ROLE_KEY` | required | Storage write on a private bucket |
| `HUMI_REASON_BUCKET` | `humi-reasons` | Target bucket |
| `WORKER_ID` | `publisher-a` | Claim stamp |
| `CONCURRENCY` | 16 | In-flight uploads |
| `CLAIM_BATCH_SIZE` | 500 | Claim size |
| `CLAIM_STALE_SECONDS` | 7200 | Reclaim |
| `MAX_RUNTIME_SECONDS` | 19800 | Slot cap |

First worker in the repo needing service role on top of `SUPABASE_DB_URL`; register the login (never the value) in the vault key inventory.

## Local

```powershell
cd workers/humi_reason_publisher
copy .env.example .env
uv sync
uv run python job.py
```

Spec parity check (no DB needed, real prod sample):

```powershell
uv run python tests/test_spec_parity.py
```

It fails if an item name ends up mapped to the wrong score column, which is the only real risk in `src/pillar_spec.py`.

## Monitor

```sql
SELECT
  count(*) FILTER (WHERE needs_reason_publish) AS pending,
  count(*) FILTER (WHERE reason_published_at IS NOT NULL) AS published,
  count(*) FILTER (WHERE reason_publish_claimed_at IS NOT NULL) AS in_flight,
  min(reason_published_at) AS oldest_publication
FROM index_humi.index_humi_agent;
```

Objects should track `published`. Add `(SELECT count(*) FROM storage.objects WHERE bucket_id = 'humi-reasons')` to compare.

## First run (2026-09-16)

Backfill of all 503 034 agents, dispatched on run `35058416219`.

| Metric | Value |
|---|---|
| Throughput | ~2 000 agents/min at `CONCURRENCY=16`, `CLAIM_BATCH_SIZE=500` |
| Object size | ~20 kB average, so the full corpus lands around 10 GB of the 100 GB Storage allowance |
| Failures | 0 in the first 23 500 |
| Stale locks | 0 |

Storage object count tracked the `reason_published_at` count exactly throughout. That equality is the check worth repeating: a persistent gap means uploads returned 200 but the matching `complete` never reached the DB.

Parity was confirmed in prod, not just in the test: a published object was downloaded back and compared as `jsonb` against `index_humi_agent`, and all four pillars matched for a real agent with non-null data.

The sha256 short-circuit could not be exercised during the backfill — every document was new. It shows up from the second run on, where most agents should log as `unchanged` and skip the upload entirely.

## Stage 2 (not built)

Port the bilingual templates of the 4 pillars to `src/render/` so the worker also produces the leaf `reason` text and the pillar `summary`, then stop writing those ~45 columns in `index_humi.pillar_*`. Requires a zero-diff parity gate first. See [PENDING](../../docs/PROCESSES.md).
