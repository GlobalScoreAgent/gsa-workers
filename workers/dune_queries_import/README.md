# Dune queries import

> Project context: [AGENTS.md](../../AGENTS.md) · [Supabase map](../../docs/SUPABASE.md) · [Architecture](../../docs/ARCHITECTURE.md)

Reference-data job: fetch four Dune queries and upsert into `wallets.*` via SQL RPCs. No wallet claim / eligibility loop.

## Tasks (one run)

| Task | Query ID | RPC | Table |
|---|---|---|---|
| cex | 7520736 | `wallets.cex_addresses_upsert` | `wallets.cex_addresses` |
| mixers | 8015078 | `wallets.mixer_addresses_upsert` | `wallets.mixer_addresses` |
| bridges | 8015106 | `wallets.bridge_addresses_upsert` | `wallets.bridge_addresses` |
| ofac_sanction | 8015112 | `wallets.ofac_sanction_addresses_upsert` | `wallets.ofac_sanction_addresses` |

Pipeline per task:

1. Paginated `GET /api/v1/query/{id}/results` (rate-limit pacing + 429 retry)
2. Fail task if 0 rows (do not wipe good data)
3. Upsert in chunks of `UPSERT_CHUNK_SIZE` (default 5000)
4. Continue to next task on failure; exit 1 if any task failed

## Schedule

GitHub Actions: days **1 and 16** at 00:00 UTC (`0 0 1,16 * *`) + `workflow_dispatch` (~every 15 days).

## Env

| Variable | Required | Default | Role |
|---|---|---|---|
| `SUPABASE_DB_URL` | Yes | — | Postgres pooler DSN |
| `DUNE_KEY` | Yes | — | Dune API key |
| `DUNE_PAGE_SIZE` | No | `10000` | Rows per Dune page |
| `DUNE_PAGE_DELAY_SECONDS` | No | `2` | Pause between Dune pages |
| `DUNE_TASK_DELAY_SECONDS` | No | `3` | Pause between tasks |
| `UPSERT_CHUNK_SIZE` | No | `5000` | Rows per RPC call |

## Local run

```powershell
cd workers/dune_queries_import
copy .env.example .env
# Set SUPABASE_DB_URL and DUNE_KEY

uv sync
uv run python job.py
```

## Monitoring SQL

CEX smoke check: expect on the order of **~36k rows** for query `7520736`.

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

RPCs / tables live in sibling repo **gsa-supabase-schema** (`wallets_*_upsert`). Deploy migrations before relying on this worker. See `supabase/docs/wallets-dune-reference-tables.md`.
