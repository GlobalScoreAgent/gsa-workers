# ethos_enrich

Worker GHA que en cada run ejecuta **dos fases secuenciales**:

1. **Fase A — history (Proceso 2):** claim `needs_history_fetch` → Goldsky GraphQL (9 señales por `profileId`) → upsert directo `ethos.*` → `complete_history_fetch`
2. **Fase B — credibility score:** wallets GSA linkeadas due (TTL **15 días**) → `POST /api/v2/score/addresses` → `ethos.official_scores`

Si no hay trabajo pendiente en una fase, esa fase termina al toque. Si ambas están vacías → **exit 0** en segundos.

## Eligibility

| Fase | Cola |
|------|------|
| History | `ethos.profile_addresses.needs_history_fetch = true` AND `history_fetched_at IS NULL` |
| Score | `profile_addresses.wallet_id IS NOT NULL` AND (`official_scores` ausente OR `next_eligible_at <= now()`) |

Soft-lock history: `history_fetch_claimed_at` / `history_fetch_claimed_by` (no confundir con `claimed_at` on-chain Ethos).

## Pipeline

```
claim_history_fetch → Goldsky ×9 → upsert ethos.* → complete_history_fetch
list_score_candidates → Ethos API bulk → upsert_official_scores (+15d)
```

## Env

| Variable | Default | Rol |
|----------|---------|-----|
| `SUPABASE_DB_URL` | required | Postgres |
| `GOLDSKY_ETHOS_URL` | Goldsky `ethos-network-base/prod` public | GraphQL |
| `ETHOS_API_BASE` | `https://api.ethos.network/api/v2` | Score API |
| `WORKER_ID` | `enrich-a` | audit claim |
| `CONCURRENCY` | `3` | perfiles history en paralelo |
| `CLAIM_BATCH_SIZE` | `10` | profiles / claim |
| `CLAIM_STALE_SECONDS` | `7200` | reclaim soft-lock |
| `SCORE_BATCH_SIZE` | `50` | addresses / bulk |
| `SCORE_TTL_DAYS` | `15` | `next_eligible_at` |
| `SCORE_THROTTLE_MS` | `200` | pause after bulk call |
| `MAX_RUNTIME_SECONDS` | `19800` | soft budget while hay trabajo |

Header Ethos: `X-Ethos-Client: gsa-ethos-enrich@1.0`.

## Local

```powershell
cd workers/ethos_enrich
uv sync
# set SUPABASE_DB_URL then:
uv run python job.py
```

## Monitoring SQL

```sql
SELECT count(*) AS history_pending
  FROM ethos.profile_addresses
 WHERE needs_history_fetch = true;

SELECT count(*) AS score_due
  FROM ethos.list_score_candidates(100000);

SELECT count(*) AS scores,
       count(*) FILTER (WHERE score IS NOT NULL) AS with_score,
       min(fetched_at) AS oldest_fetch,
       max(fetched_at) AS newest_fetch
  FROM ethos.official_scores;
```

## Schema

Migración: `gsa-supabase-schema` → `20260806010000_ethos_enrich_worker.sql`  
Docs: `supabase/docs/ethos-erc8004-linking.md`

## Workflow

`.github/workflows/ethos-enrich.yml` — cron `0 0,6,12,18 * * *` UTC + `workflow_dispatch`.
