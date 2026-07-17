# Dune queries import

> Project context: [AGENTS.md](../../AGENTS.md) · [Supabase map](../../docs/SUPABASE.md) · [Architecture](../../docs/ARCHITECTURE.md) · [Ops](../../docs/OPS.md)

Reference-data job: fetch up to four Dune queries and upsert into `wallets.*` via SQL RPCs. No wallet claim / eligibility loop.

Former name: `cex_addresses_import` (workflow `cex-addresses-import.yml`).

## Tasks (one run)

| Task | Query ID | RPC | Table | Typical size |
|---|---|---|---|---|
| cex | 7520736 | `wallets.cex_addresses_upsert` | `wallets.cex_addresses` | ~36k |
| mixers | 8015078 | `wallets.mixer_addresses_upsert` | `wallets.mixer_addresses` | ~40 |
| bridges | 8015106 | `wallets.bridge_addresses_upsert` | `wallets.bridge_addresses` | ~260 |
| ofac_sanction | 8015112 | `wallets.ofac_sanction_addresses_upsert` | `wallets.ofac_sanction_addresses` | ~40 |

Pipeline per task:

1. Paginated `GET /api/v1/query/{id}/results` (rate-limit pacing + 429/503 retry)
2. Fail task if 0 rows (do not wipe good data)
3. Upsert in chunks of `UPSERT_CHUNK_SIZE` (default 5000)
4. SQL RPCs `DISTINCT ON (address, chain)` so duplicate keys in one batch do not raise `CardinalityViolation`
5. Continue to next task on failure; exit 1 if any selected task failed

Large result sets: keep Dune queries bounded. An unbounded Bridges query (~1M+ rows) can exhaust Dune datapoint quotas (HTTP 402) before upsert starts.

## Schedule

GitHub Actions: days **1 and 16** at 00:00 UTC (`0 0 1,16 * *`) + `workflow_dispatch` (~every 15 days). GHA timeout: **90 minutes**.

### Partial re-run (`DUNE_TASKS`)

Optional filter so you can refresh one query without spending Dune credits on the others (useful after a 402 or a query edit).

| How | Example |
|---|---|
| Env | `DUNE_TASKS=bridges` or `DUNE_TASKS=mixers,ofac_sanction` |
| Actions UI | **Run workflow** → input `dune_tasks` |
| CLI | `gh workflow run dune-queries-import.yml -f dune_tasks=bridges` |

Empty / unset = all four tasks. Unknown names → exit 1.

## Env

| Variable | Required | Default | Role |
|---|---|---|---|
| `SUPABASE_DB_URL` | Yes | — | Postgres pooler DSN |
| `DUNE_KEY` | Yes | — | Dune API key |
| `DUNE_PAGE_SIZE` | No | `10000` | Rows per Dune page |
| `DUNE_PAGE_DELAY_SECONDS` | No | `2` | Pause between Dune pages |
| `DUNE_TASK_DELAY_SECONDS` | No | `3` | Pause between tasks |
| `UPSERT_CHUNK_SIZE` | No | `5000` | Rows per RPC call |
| `DUNE_TASKS` | No | (all) | Comma-separated task names |

## Local run

```powershell
cd workers/dune_queries_import
copy .env.example .env
# Set SUPABASE_DB_URL and DUNE_KEY

uv sync
uv run python job.py

# Single task:
$env:DUNE_TASKS="bridges"
uv run python job.py
```

## Monitoring SQL

```sql
SELECT 'cex' AS src, count(*) AS rows, max(updated_at) AS last_updated FROM wallets.cex_addresses
UNION ALL
SELECT 'mixers', count(*), max(updated_at) FROM wallets.mixer_addresses
UNION ALL
SELECT 'bridges', count(*), max(updated_at) FROM wallets.bridge_addresses
UNION ALL
SELECT 'ofac', count(*), max(updated_at) FROM wallets.ofac_sanction_addresses;
```

## Schema dependency

Deploy in **gsa-supabase-schema** before relying on this worker:

- `wallets.cex_addresses_upsert` (CEX table pre-existed)
- `wallets_dune_reference_tables` — mixer / bridge / ofac tables + upserts
- `wallets_dune_upsert_dedupe` — `DISTINCT ON` in upsert RPCs

Docs: `supabase/docs/wallets-dune-reference-tables.md`.
