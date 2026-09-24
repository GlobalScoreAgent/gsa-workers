# HUMI Reason Publisher

> Project context: [AGENTS.md](../../AGENTS.md) · [Process catalog](../../docs/PROCESSES.md) · [Supabase map](../../docs/SUPABASE.md) · [Architecture](../../docs/ARCHITECTURE.md)

**Status: live after schema deploy** (cron `0 0,6,12,18 * * *` UTC + `workflow_dispatch`).

Publishes the HUMI narrative aggregate of each agent as a JSON object in the **private** Supabase Storage bucket `humi-reasons`, so `index_humi.index_humi_agent` stops carrying ~12 kB of TOAST per row through every recalculation.

**ADR (Etapa 1 Storage):** vault `08 - Decisiones/2026-09-15 - Toast JSON frios a Supabase Storage`  
**ADR (Etapa 2 render):** vault `08 - Decisiones/2026-09-24 - HUMI reason Stage 2 render en Python`  
**Schema:** `gsa-supabase-schema` → `supabase/docs/toast-cold-storage.md` (`20260916010000_humi_reason_publish_claim.sql`, `20260916010100_agent_index_humi_calculate_reason_publish_flag.sql`)  
**Vault ops:** `12 - Github Worker/HUMI Reason Publisher/`  
**GHA host:** [`GlobalScoreAgent/gsa-workers`](https://github.com/GlobalScoreAgent/gsa-workers) (DB-light). MichBarbarian copy is schedule-disabled.

The worker is a **renderer, not an engine**: every `*_score` keeps being computed and persisted by SQL. Stage 2 only chooses narrative text (`src/render/`); it does not recompute scores.

## Why a fixed cron and not "when the HUMI lane finishes"

The lane has no stable finish time. It always starts at 18:00 UTC (`job_control.start_daily_index_pipeline`) but measured in `cron.job_run_details` it took 12 minutes on quiet days (Sep 4-8) and close to 24 hours on loaded ones (Sep 9, 11, 13). Anchoring the worker to that moment would miss by up to a day.

The claim is idempotent and content-hashed, so running mid-lane is harmless: it drains whatever is flagged and the next run picks up the rest. Consequence: the narrative lags the score, accepted silently (the UI shows the previous version, same as it already does between daily cycles).

The cron interval puts that lag at 6 h on paper, but **measure it against reality, not the cron**. Scheduled runs on this account fire 3–5 h late across every workflow (see [OPS.md](../../docs/OPS.md#scheduled-runs-fire-hours-late)), which pushes the practical worst case closer to 10 h.

## Pipeline

```
claim_reason_publish (SKIP LOCKED + soft-lock)
  → SELECT pillar_* scores (Stage 2: no *_reason columns)
  → fetch_render_contexts (same summary inputs the SQL calculates use)
  → assemble via src/render/* (HUMI_REASON_RENDER=1) or copy jsonb (Stage 1 legacy)
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
| `HUMI_REASON_RENDER` | `0` (GHA: `1`) | Stage 2: generate reasons in Python |
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

Spec + render parity (no DB needed for the shipped fixtures):

```powershell
uv run python tests/test_spec_parity.py
uv run python tests/test_render_parity.py
```

`test_spec_parity` fails if an item name maps to the wrong score column. `test_render_parity` fails if Python reasons diverge from the SQL fixture (after normalizing `last_calculated`).

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

## Initial backfill (2026-09-16)

Took three runs, not one. Every agent in the table ended up published, with zero errors and zero stale locks.

| Run | Duration | Processed | Uploaded | Unchanged | Outcome |
|---|---|---|---|---|---|
| `35058416219` (dispatch) | 5h30m | 309 500 | 309 451 | 49 | hit `MAX_RUNTIME_SECONDS` |
| `35085243105` (schedule) | 2h49m | 195 064 | 195 064 | 0 | drained the rest |
| `35117566714` (schedule) | 13s | 0 | 0 | 0 | `queue empty` |

Final state: 504 428 agents, 504 428 objects, 9 810 MB — about 20 kB per object, roughly 10 % of the 100 GB Storage allowance.

**Throughput: ~1 030 agents/min sustained** (938/min on the first run, 1 230/min on the second) at `CONCURRENCY=16`, `CLAIM_BATCH_SIZE=500`. A ten-minute sample early in the first run showed ~2 000/min; that was a favourable window and not representative — size capacity off the sustained figure.

The bottleneck is Storage round-trip latency, roughly 0.5 s per object, not the DB or CPU. Raising `CONCURRENCY` scales nearly linearly; raising `CLAIM_BATCH_SIZE` does nothing.

### What the backfill proved

- **Parity in prod, not just in the test.** A published object was downloaded back and compared as `jsonb` against `index_humi_agent`; all four pillars matched for a real agent with non-null data.
- **The sha256 short-circuit fires.** 49 agents in the first run were re-flagged by the lane mid-run and skipped the upload because their text had not changed. Small sample, but the mechanism works, and the hash is persisted on all 504 428 rows.
- **The flag path works.** The lane recalculated 62 359 agents that day; all ended up published after their recalculation, so `calculated_at > reason_published_at` returns zero rows.
- Storage object count tracked `reason_published_at` exactly throughout. A persistent gap would mean uploads returned 200 but the matching `complete` never reached the DB.

## Stage 2 (live) — render in Python

Live since **2026-09-24** with `HUMI_REASON_RENDER=1` on the GSA host workflow. Leaf `reason` text and `pillar_summary` come from `src/render/` (history / information / measure / usage) using scores + the same summary inputs the SQL calculates use. The worker no longer copies `*_reason` jsonb from `pillar_*` when the flag is on.

SQL **still writes** those columns and the cold copy on `index_humi_agent` (stop-write / `DROP` / `pg_repack` = separate schema process). Document shape stays `schema: 1`; rendered summaries omit `last_calculated` so the sha short-circuit stays meaningful once the corpus is homogeneous.

The product web already hydrates narrative from Storage only — Stage 2 does not require a front change. Backfill is optional for UI correctness; it exists to homogenize objects before a future column DROP.

### Stage 2 corpus republish (2026-09-24, in progress)

Re-flagged the full table (`needs_reason_publish = true`, `reason_content_sha256 = null`) and drained with `workflow_dispatch` on `GlobalScoreAgent/gsa-workers`. Sustained ~750–800 agents/min (Storage-bound, same as Stage 1). Job timeout 360 min → expect **2–3 runs** for ~529 k agents.

| Check | SQL / action |
|---|---|
| Queue | `count(*) FILTER (WHERE needs_reason_publish)` |
| Stage 2 published | `count(*) FILTER (WHERE reason_content_sha256 IS NOT NULL)` (sha was nulled on re-flag) |
| Done | `needs_reason_publish = 0` and Storage object count ≈ published |
| Next slot | `gh workflow run humi-reason-publisher.yml --repo GlobalScoreAgent/gsa-workers` |

Canceling mid-run is fine for product (old Stage 1 objects remain valid). Leaving the flag set means cron slots continue the drain.