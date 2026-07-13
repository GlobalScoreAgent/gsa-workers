# Token prices import (DexScreener → CoinGecko enrich)

> Project context: [AGENTS.md](../../AGENTS.md) · [Supabase map](../../docs/SUPABASE.md) · [Architecture](../../docs/ARCHITECTURE.md)

Reference-data job: find distinct unpriced ERC-20s in `wallets.wallet_token_positions`, resolve USD via cache → **DexScreener** → **CoinGecko**, upsert `wallets.token_prices`, then apply to positions.

Chain platform slugs come from `erc_8004.chains.subdomain_dexscreener` / `subdomain_coingecko` (not hardcoded).

## Pipeline

1. Load chain subdomains from `erc_8004.chains`
2. `DISTINCT (chain_id, contract)` where `has_price_error`, not spam, not `native`
3. Skip rows with fresh cache (`fetched_at` within `PRICE_CACHE_TTL_HOURS`, including misses)
4. Per chain, per API batch:
   - DexScreener (~30 contracts/request) → **upsert hits immediately**
   - CoinGecko (~100 contracts/request) for remainder → **upsert hits immediately**
   - Upsert misses + **mark positions** (`has_price_error=false`, `quality_reason=unknown_token_dex_coingecko_defillama`) so they leave the enrich queue
   - `wallet_token_positions_apply_prices()` after each chain
5. Final `apply_prices` at end

`has_price_error=false` + `quality_reason=unknown_token_dex_coingecko_defillama` means Llama/Dex/CG all failed to price the token — not a transient worker error. Candidates require `has_price_error=true`, so those rows are not reprocessed on the next run.

CoinGecko auth uses header from `COINGECKO_KEY` + `COINGECKO_API_PLAN` (`demo` → `x-cg-demo-api-key` / `api.coingecko.com`; `pro` → `x-cg-pro-api-key` / `pro-api.coingecko.com`).

## Schedule

Manual `workflow_dispatch` (optional cron later). Requires GitHub secret `COINGECKO_KEY`.

## Env

| Variable | Required | Default | Role |
|---|---|---|---|
| `SUPABASE_DB_URL` | Yes | — | Postgres |
| `COINGECKO_KEY` | Yes | — | CoinGecko Demo/Pro key |
| `COINGECKO_API_PLAN` | No | `demo` | `demo` or `pro` |
| `PRICE_CACHE_TTL_HOURS` | No | `24` | Skip API if cache fresh |
| `MIN_LIQUIDITY_USD` | No | `1000` | Dex pair floor |
| `MAX_RUNTIME_SECONDS` | No | `19800` | Soft stop on fetch |

## Local run

```powershell
cd workers/token_prices_import
copy .env.example .env
# Set SUPABASE_DB_URL and COINGECKO_KEY

uv sync
uv run python job.py
```

## Monitoring

```sql
SELECT source, count(*), count(*) FILTER (WHERE price_usd IS NOT NULL) AS with_price
FROM wallets.token_prices
GROUP BY 1;

SELECT count(*) FILTER (WHERE has_price_error AND COALESCE(token_quality,'') <> 'spam'
  AND contract_address <> 'native') AS still_unpriced
FROM wallets.wallet_token_positions;
```

## Schema

See `gsa-supabase-schema`: `chains_price_subdomains`, `wallets_token_prices_spot_cache`.

## Legacy

Dune daily import into this table is retired. `walcert.token_prices` history is unchanged.
