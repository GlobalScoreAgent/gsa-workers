# Agent Series Export

> Project context: [AGENTS.md](../../AGENTS.md) · [Process catalog](../../docs/PROCESSES.md) · [Supabase map](../../docs/SUPABASE.md) · [Architecture](../../docs/ARCHITECTURE.md)

**Status: schema deployed 2026-09-17, worker pending first full pass** (cron `0 1 * * *` UTC + `workflow_dispatch`).

Publishes each agent's 30-day nonce/balance tree as a JSON object in the **public** Supabase Storage bucket `agent-series`, and persists the five scalars HUMI needs in `erc_8004.agent_tx_scalars`.

**ADR:** vault `08 - Decisiones/2026-09-16 - Worker Agent Series Export a Storage`
**Schema:** `gsa-supabase-schema` → `supabase/docs/agent-series-export-storage.md` (`20260917010000_agent_tx_scalars.sql`, `20260917010100_agent_series_export_claim.sql`, `20260917010200_agent_series_bucket_and_cycle.sql`)
**Vault ops:** `12 - Github Worker/Agent Series Export/`

## Why this exists

The `series` stage of `job_control.wallet_tx_rollup_pipeline` writes `wallet_transactions.nonce_last_30_days` / `balance_last_30_days` one wallet at a time. Each write rewrites the row, its TOAST chunks and the WAL, ~450 k times per pass, for a stage that needs about 29 h of compute to cover a day. It never closes: 262 913 wallets were queued when this worker was designed.

This worker does not write that JSON at all. Postgres builds the tree in a `SELECT` straight from `erc_8004.wallet_daily_metrics`, the worker streams it out and PUTs it to Storage. No `jsonb` UPDATE, no TOAST churn.

## Where each field comes from

The claim **cannot** read the 30-day arrays from `wallet_transactions`: that is the stalled stage's output. Per field:

| Field | Source |
|---|---|
| per chain 30-day series | `wallet_daily_metrics`, densified against `generate_series(series_start, as_of)` with `{"value": null}` in the gaps |
| `series_start` | `GREATEST(as_of - 30, first_snapshot_date)`, same bound as `wallet_rollup_series` |
| `nonce_current`, `balance_current`, `wallet_category` | `wallet_transactions` (the `currents` stage, which does close daily) |
| wallet and agent aggregation | the same `wallet_summary_merge_*` functions the MVs use, so parity is by construction |

Balance maps are keyed by `erc_8004.chains.symbol_wallet_process` (`ethereum`, `bsc`, …), not by token symbol or `chain_id`. Sepolia and Solana have that column `NULL`, so they contribute to nonce totals but disappear from balance maps — deliberately preserved.

## Pipeline

```
agent_series_cycle_open(as_of)                 all lanes, idempotent
  → agent_tx_scalars_refresh(as_of, batch, cursor)   lane a only, loops until scanned = 0
  → agent_series_claim(batch, as_of, worker, stale)  SKIP LOCKED + soft-lock, returns the tree
      document NULL → ack without a PUT (agent has no wallet / no wallet_transactions row)
      otherwise     → POST /storage/v1/object/agent-series/agents/{id}.json (x-upsert)
  → agent_series_ack(rows, as_of)
  → agent_series_cycle_close(as_of)            closed only if the queue is empty
```

`as_of` is resolved **in the database** (`(now() AT TIME ZONE 'utc')::date - 1`) so all three lanes agree even if they start minutes apart.

The document travels as `document::text` and is uploaded byte for byte. Parsing it into Python would turn balance `numeric` values into floats and silently drop decimals.

### Why there is no sha256 short-circuit

The publisher skips a PUT when the content hash matches. Here it cannot fire: the claim only returns agents whose `series_export_as_of` is behind the target, and the window moves every day, so the content always differs. `series_export_sha256` is still persisted for auditing and for comparing Storage against the DB.

### Queue without a boolean flag

The queue is `series_export_as_of IS NULL OR series_export_as_of < as_of`. A boolean flag would need a daily UPDATE over 521 k rows to reopen the cycle, which is exactly the write amplification this design removes. Exported agents sort to the end of `idx_agents_series_export_claim`, so the claim always finds pending rows at the front instead of paying a growing skip.

### Failure handling

Failed rows **keep** their soft-lock instead of being released, same reasoning as the publisher: releasing them would make the next claim return the same batch and the run would spin. They return to the queue when `CLAIM_STALE_SECONDS` expires. Three consecutive batches with zero successful rows abort with exit 1.

```sql
SELECT erc_8004.agent_series_release(ARRAY[123, 456]::bigint[]);
```

## Document

`agent-series/agents/{agent_id}.json`, public bucket (served by CDN).

```json
{
  "agent_id": 51,
  "as_of": "2026-09-16",
  "nonce_total": 376,
  "balance_total": { "ethereum": 0.0123 },
  "nonce_last_30_days": [ { "date": "2026-08-17", "value": 375 } ],
  "balance_last_30_days": [ { "date": "2026-08-17", "balances": { "ethereum": 0.0123 } } ],
  "transactional_wallets_details": [
    {
      "wallet_address": "0x…",
      "general_nonce_total": 376,
      "general_balance_total": { "ethereum": 0.0123 },
      "general_nonce_last_30_days": [ { "date": "2026-08-17", "value": 375 } ],
      "general_balance_last_30_days": [ { "date": "2026-08-17", "balances": { } } ],
      "chains": [
        {
          "chain_id": 1,
          "chain_name": "Ethereum Mainnet",
          "wallet_category": "Active",
          "nonce_current": 376,
          "balance_current": 0.0123,
          "nonce_last_30_days": [ { "date": "2026-08-17", "value": 375 } ],
          "balance_last_30_days": [ { "date": "2026-08-17", "value": 0.0123 } ]
        }
      ]
    }
  ]
}
```

`transactional_wallets_details` reproduces `erc_8004.agent_summary_tx_details` key for key. Wallets are ordered by `wallet_address`, chains by `chain_id`.

Agents without a valid wallet, or whose wallets have no `wallet_transactions` row, get no object at all. They are still acked so the cycle can close. If an agent loses all its wallets, its old object stays in the bucket; cleaning that up is not built.

## Env

| Variable | Default | Role |
|---|---|---|
| `SUPABASE_DB_URL` | required | Pooler DSN |
| `SUPABASE_URL` | required | Project URL for the Storage REST API |
| `SUPABASE_SERVICE_ROLE_KEY` | required | Storage write |
| `AGENT_SERIES_BUCKET` | `agent-series` | Target bucket |
| `WORKER_ID` | `exporter-a` | Claim stamp |
| `RUN_SCALARS` | `false` | Step 1; enabled on lane a only |
| `SCALARS_BATCH_SIZE` | 5000 | Agents scanned per `agent_tx_scalars_refresh` call |
| `CONCURRENCY` | 16 | In-flight uploads |
| `CLAIM_BATCH_SIZE` | 500 | Claim size (documents average ~6 kB, peak 41 kB) |
| `CLAIM_STALE_SECONDS` | 7200 | Reclaim |
| `MAX_RUNTIME_SECONDS` | 19800 | Slot cap |

## Local

```powershell
cd workers/agent_series_export
copy .env.example .env
uv sync
uv run python job.py
```

## Monitor

```sql
-- Cycle
SELECT * FROM job_control.agent_series_export_cycle ORDER BY as_of DESC LIMIT 7;

-- Queue against the current target
SELECT
  count(*) AS agents,
  count(*) FILTER (WHERE series_export_as_of = (now() AT TIME ZONE 'utc')::date - 1) AS exported_today,
  count(*) FILTER (WHERE series_export_as_of IS NULL) AS never_exported,
  count(*) FILTER (WHERE series_export_claimed_at IS NOT NULL) AS in_flight,
  min(series_export_as_of) AS oldest_cycle
FROM erc_8004.agents;

-- Objects vs agents with a document
SELECT count(*) AS objects, pg_size_pretty(sum((metadata->>'size')::bigint)) AS total_size
FROM storage.objects WHERE bucket_id = 'agent-series';

-- Scalars freshness
SELECT as_of, count(*) FROM erc_8004.agent_tx_scalars GROUP BY as_of ORDER BY as_of DESC;
```

## Sizing

Measured against prod on 2026-09-17: the claim returns 500 agents in 1.1 s (446 agents/s) and 200 in 0.8 s, so the fixed cost per call dominates and larger batches are cheaper. Documents average 5.8 kB and peak at 41 kB, which puts a full pass at roughly 2 GB in the bucket.

The database is therefore **not** the bottleneck — Storage round-trip latency is, the same ~0.5 s per object the publisher measured. At `CONCURRENCY=16` per lane, early sizing assumed two lanes would finish ~367 k objects in ~3 h. By 2026-09-24 the universe was ~534 k agents and two lanes left ~141 k one day behind (`incomplete` cycle) inside the 5.5 h soft budget — so the matrix added `exporter-c` (three lanes, still one cron at 01:00 UTC, still before the 12-18 UTC MV blackout).

## Known difference against the old chain

The stored series carries gaps that the incremental path never backfills: once a day is written as `{"value": null}`, a metric that lands later never fills it. Rebuilding from `wallet_daily_metrics` fills them. Measured on a 17 557-point sample of fresh wallets: 513 gaps filled, **zero** values lost and zero values changed. The export is strictly closer to the source.

## Out of scope

Migrating consumers (web, HUMI) to read the bucket and `agent_tx_scalars`, dropping the JSON chain, enabling the `wallet_daily_metrics` purge by watermark, and quarantining `agent_summary_tx_balance` / `agent_chain_flagged`. All explicit in the ADR.
