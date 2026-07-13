# Wallet token portfolio discovery

> Project context: [AGENTS.md](../../AGENTS.md) · [Supabase map](../../docs/SUPABASE.md) · [Architecture](../../docs/ARCHITECTURE.md)

Initial fungible portfolio fill: reads `wallets.wallet_token_contracts`, values balances via Alchemy + **DeFiLlama only** (no `token_prices`), **INSERT**s into `wallets.wallet_token_positions`.

Calculation lives in `src/portfolio_calc.py` (reusable by a future 15-day updater).

## Eligibility

```sql
does_need_portfolio_discovery IS DISTINCT FROM FALSE
AND does_need_discovery_contracts = FALSE
AND COALESCE(has_discovery_contracts_error, FALSE) IS NOT TRUE
AND chains.subdomain_alchemy IS NOT NULL
```

## Pipeline

1. Claim `wallet_transactions` rows
2. Load contracts from `wallet_token_contracts`
3. `portfolio_calc.calculate_fungible_positions` (native + ERC-20 amounts + DeFiLlama)
4. `wallets.wallet_token_positions_insert` (INSERT … ON CONFLICT DO NOTHING)
5. Mark portfolio discovery done

Native row uses `contract_address = 'native'`. Missing DeFiLlama price → `has_price_error = true`.

### `token_quality` / `quality_reason`

| `token_quality` | When |
|---|---|
| `priced` | DeFiLlama returned price > 0 |
| `spam` | No price + symbol matches claim/URL/telegram heuristics (ERC-20 only; native never spam) |
| `unpriced` | No price and not spam |

`quality_reason` examples: `claim_in_symbol`, `url_in_symbol`, `telegram_in_symbol`, `no_defillama_price` (null when priced).

Polygon native uses DeFiLlama key `coingecko:polygon-ecosystem-token` (POL).

### Re-run after pricing / quality changes

Insert is `ON CONFLICT DO NOTHING`, so schema/worker fixes do not rewrite existing rows. After deploy:

1. Apply schema migration `wallet_token_positions_quality`
2. Deploy this worker
3. Run `gsa-supabase-schema/supabase/scripts/wallet_token_portfolio_discovery_reset.sql` (TRUNCATE positions + re-flag queue)
4. `workflow_dispatch` this workflow

## Environment

| Variable | Default | Description |
|---|---|---|
| `SUPABASE_DB_URL` | required | Postgres |
| `ALCHEMY_FREE_KEY` | required | Alchemy (`ALCHEMY_KEY` fallback) |
| `WORKER_ID` | `discovery-a` | Suffix for claimed_by |
| `CONCURRENCY` | 5 | Parallel rows |
| `CLAIM_BATCH_SIZE` | 25 | |
| `CLAIM_STALE_SECONDS` | 7200 | |
| `MAX_RUNTIME_SECONDS` | 19800 | |
