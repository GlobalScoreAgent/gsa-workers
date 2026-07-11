# Token prices import

> Project context: [AGENTS.md](../../AGENTS.md) · [Supabase map](../../docs/SUPABASE.md) · [Architecture](../../docs/ARCHITECTURE.md)

Reference-data job: fetch the latest Dune token-price list and insert into `wallets.token_prices` via one SQL RPC. No wallet claim / eligibility loop.

## Pipeline

1. `GET https://api.dune.com/api/v1/query/{DUNE_QUERY_ID}/results` (paginated)
2. Fail if 0 rows (do not wipe good data)
3. `SELECT wallets.token_prices_upsert(rows::jsonb)`
4. Exit 0 on success, 1 on Dune/DB failure

Default query id: **7526826**.

Conflict policy: `ON CONFLICT (contract_address, blockchain, price_date) DO NOTHING` (same as legacy `walcert.token_prices_process`).

If a single jsonb payload is too large for the pooler, call the same RPC in chunks of N rows (same insert semantics). Start with one call (~2k rows typical).

## Schedule

GitHub Actions: daily at **01:00 UTC** (`0 1 * * *`) + `workflow_dispatch` (same cadence as the former walcert token-prices import cron).

## Env

| Variable | Required | Default | Role |
|---|---|---|---|
| `SUPABASE_DB_URL` | Yes | — | Postgres pooler DSN |
| `DUNE_KEY` | Yes | — | Dune API key |
| `DUNE_QUERY_ID` | No | `7526826` | Dune query id |
| `DUNE_PAGE_SIZE` | No | `10000` | Rows per Dune page |

## Local run

```powershell
cd workers/token_prices_import
copy .env.example .env
# Set SUPABASE_DB_URL and DUNE_KEY

uv sync
uv run python job.py
```

## Monitoring SQL

```sql
SELECT count(*) AS rows, max(price_date) AS max_price_date
FROM wallets.token_prices;

SELECT blockchain, count(*) AS n
FROM wallets.token_prices
GROUP BY 1
ORDER BY n DESC
LIMIT 10;
```

## Schema dependency

RPC lives in sibling repo **`gsa-supabase-schema`**: `wallets.token_prices_upsert(p_rows jsonb)`. Deploy that migration before relying on this worker. See also `supabase/docs/wallets-token-prices-upsert.md` in that repo.

## Legacy

Replaces Edge `walcert-update-token-prices` + SQL wrappers `walcert.token_prices_import_data` / `walcert.token_prices_process` + pg_cron jobs `walcert_token_prices_*`. Staging table `walcert.token_prices_imported_data` is no longer written by this worker.
