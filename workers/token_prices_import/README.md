# Token prices import (DexScreener → CoinGecko enrich)

> Project context: [AGENTS.md](../../AGENTS.md) · [Supabase map](../../docs/SUPABASE.md) · [Architecture](../../docs/ARCHITECTURE.md)

Reference-data job: find distinct unpriced ERC-20s in `wallets.wallet_token_positions`, resolve USD via cache → **DexScreener** → **CoinGecko**, upsert `wallets.token_prices`, then apply to positions.

Chain platform slugs come from `erc_8004.chains.subdomain_dexscreener` / `subdomain_coingecko` (not hardcoded).

## Pipeline

1. Load chain subdomains from `erc_8004.chains`
2. `DISTINCT (chain_id, contract)` where `has_price_error`, not spam, not `native`
3. Skip rows with fresh cache (`fetched_at` within `PRICE_CACHE_TTL_HOURS`, including misses)
4. DexScreener (min liquidity) → else CoinGecko batch by platform
5. `wallets.token_prices_upsert` (`source` = `dexscreener` | `coingecko` | `miss`)
6. `wallets.wallet_token_positions_apply_prices()`

## Schedule

Manual `workflow_dispatch` (optional cron later). Requires GitHub secret `COINGECKO_KEY`.

## Env

| Variable | Required | Default | Role |
|---|---|---|---|
| `SUPABASE_DB_URL` | Yes | — | Postgres |
| `COINGECKO_KEY` | Yes | — | CoinGecko Demo/Pro key |
| `PRICE_CACHE_TTL_HOURS` | No | `24` | Skip API if cache fresh |
| `MIN_LIQUIDITY_USD` | No | `1000` | Dex pair floor |
| `UPSERT_CHUNK_SIZE` | No | `500` | Rows per upsert RPC |
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
