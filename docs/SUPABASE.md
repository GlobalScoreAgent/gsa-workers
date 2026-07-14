# Supabase / Postgres interaction

Workers connect with **direct Postgres** via `SUPABASE_DB_URL` (`psycopg`), not supabase-js or Edge Functions. Schema of truth for wallet claim jobs: `erc_8004`. Reference-data imports (CEX addresses, token prices) use schema `wallets`.

Schema migrations and snapshot/upsert SQL live in the sibling repo **`gsa-supabase-schema`** (functions `wallet_apply_*_snapshot`, `wallets.cex_addresses_upsert`, `wallets.token_prices_upsert`, `wallet_token_positions_apply_prices`, `wallet_token_positions_mark_price_misses`, `wallet_token_contracts_upsert`, `wallet_token_positions_insert`, `wallet_lp_positions_upsert`, triggers, indexes). Code of truth for claim/save SQL in this repo: each worker’s `src/db.py`. Process catalog: [PROCESSES.md](./PROCESSES.md). LP refresh (15d) still pending: [PENDING_LP_POSITIONS.md](./PENDING_LP_POSITIONS.md).

## Connection

| Setting | Value |
|---|---|
| Env | `SUPABASE_DB_URL` (pooler DSN) |
| Client | `psycopg` 3, one long-lived connection per run |
| `statement_timeout` | `300s` (set on connect) |
| Retries | Up to 3 on `OperationalError` / `InterfaceError` / `QueryCanceled` / `DeadlockDetected` (reconnect on connection errors) |

## Tables

| Table | Role |
|---|---|
| `erc_8004.wallets` | Claim queue, JSON payloads, status, `next_eligible_at` |
| `erc_8004.chains` | Active chains + `subdomain_alchemy` for Alchemy fallback |
| `erc_8004.wallet_transactions` | Daily snapshot: current nonce/balance + 30d history + category |
| `erc_8004.chain_nonces` | Daily snapshot: per-chain daily nonce totals (incremental) |
| `erc_8004.wallet_owner_details` | Monthly + origin snapshots: owner metrics / first tx |
| `wallets.cex_addresses` | CEX address reference list (Dune import) |
| `wallets.token_prices` | Spot USD cache PK `(chain_id, contract)`; Dex/CG enrich |
| `wallets.wallet_token_contracts` | ERC-20 contracts with balance > 0 per wallet+chain (discovery) |
| `wallets.wallet_token_positions` | Fungible positions (native=`'native'` + ERC-20); initial INSERT discovery |
| `wallets.lp_pools` | Classic LP scan targets (`active` toggle); seeded Aerodrome V1 on Base |
| `wallets.wallet_lp_positions` | LP snapshots (UniV3 NFT + classic); PK `(wallet_id, chain_id, position_kind, nft_manager, token_id, pool)`; FKs to `wallets`/`chains`; `calculated_at` for future 15d refresh |

## Per-worker column map

| Worker | Valid flag | Schedule column | Payload | Status column | Timestamp |
|---|---|---|---|---|---|
| **daily** | `is_valid_import_current_nonce_and_balance_daily` | `import_nonce_and_balance_daily_next_eligible_at` | `import_current_nonce_and_balance_daily_json` | `import_nonce_and_balance_daily_last_status` | `import_nonce_and_balance_daily_at` |
| **monthly** | `is_valid_import_current_nonce_and_balance_monthly` | `import_nonce_and_balance_monthly_next_eligible_at` | `import_current_nonce_and_balance_monthly_json` | `import_nonce_and_balance_monthly_last_status` | `import_nonce_and_balance_monthly_at` |
| **origin** | `is_valid_import_current_nonce_and_balance_monthly` | `import_wallet_history_next_eligible_at` | `import_wallet_history_data` | `import_wallet_history_status` | `import_wallet_history_at` |

Daily also uses claim metadata:

- `import_nonce_and_balance_daily_claimed_at`
- `import_nonce_and_balance_daily_claimed_by` (`WORKER_ID`)

### Token contracts discovery (`wallet_transactions`)

| Column | Role |
|---|---|
| `does_need_discovery_contracts` | `NULL`/`true` = pending; `false` = attempted (success or error) |
| `discovery_contracts_claimed_at` | In-flight claim lock; after attempt kept as last-attempt timestamp (`NOW()`) |
| `discovery_contracts_claimed_by` | Audit id `wallet_token_contracts_discovery/gha:{WORKER_ID}` (kept after attempt) |
| `has_discovery_contracts_error` | `TRUE` if last attempt failed |
| `discovery_contracts_message_error` | Last error text; `NULL` on success |

Eligibility: flag pending **and** `chains.subdomain_alchemy` non-empty. New `wallet_transactions` inserts get the flag from trigger `trg_wallet_transactions_discovery_flag_bi`. On process error the worker sets flag `FALSE` and fills the error columns so the queue does not re-claim the same row forever.

### Token portfolio discovery (`wallet_transactions`)

| Column | Role |
|---|---|
| `does_need_portfolio_discovery` | Pending after contract discovery done |
| `portfolio_discovery_claimed_at` | Claim lock / last attempt |
| `portfolio_discovery_claimed_by` | `wallet_token_portfolio_discovery/gha:{WORKER_ID}` |
| `has_portfolio_discovery_error` | Last attempt failed |
| `portfolio_discovery_message_error` | Error text |

Trigger `trg_wallet_transactions_portfolio_flag_bu` sets portfolio pending when contract discovery completes successfully.

### LP positions discovery (`wallet_transactions`)

| Column | Role |
|---|---|
| `does_need_lp_discovery` | Pending after portfolio discovery done |
| `lp_discovery_claimed_at` | Claim lock / last attempt |
| `lp_discovery_claimed_by` | `wallet_lp_positions_discovery/gha:{WORKER_ID}` |
| `has_lp_discovery_error` | Last attempt failed |
| `lp_discovery_message_error` | Error text |

Trigger `trg_wallet_transactions_lp_flag_bu` sets LP pending when portfolio discovery completes successfully.

### `next_eligible_at` semantics

| Value | Meaning |
|---|---|
| `-infinity` | Never processed / force re-queue; eligible now |
| `<= NOW()` | Due for claim |
| `> NOW()` | In-flight (Pending claim window) or already scheduled |
| `NULL` | Out of scope (`is_valid` false) |

Eligibility predicate (all workers):

```sql
is_valid_* IS TRUE
AND *_next_eligible_at <= NOW()
```

### Status lifecycle

`NULL` / eligible → **`Pending`** (claim) → **`Completed`** or **`Error`** (save) → **`Processed`** (snapshot RPC success).

Snapshot failure after Completed → status **`Error`**.

## Snapshot RPCs

Called inline by the worker after a successful `Completed` save:

| Worker | Function | Writes |
|---|---|---|
| daily | `erc_8004.wallet_apply_daily_snapshot(p_wallet_id)` | `wallet_transactions`, `chain_nonces`; status → `Processed` |
| monthly | `erc_8004.wallet_apply_monthly_snapshot(p_wallet_id)` | `wallet_owner_details` (nonce/balance/type); status → `Processed` |
| origin | `erc_8004.wallet_apply_owner_history_snapshot(p_wallet_id)` | `wallet_owner_details.first_transaction_at`; status → `Processed` |

Canonical SQL / migrations: `gsa-supabase-schema/supabase/migrations/` and `supabase/scripts/wallet_apply_*.sql`.

**Do not** re-enable the old pg_cron jobs that used to do this work (see [DEPRECATION.md](./DEPRECATION.md)).

## Reference-data RPCs

| Worker | Function | Writes |
|---|---|---|
| cex import | `wallets.cex_addresses_upsert(p_rows jsonb)` | `wallets.cex_addresses` (`ON CONFLICT (address, chain)`) |
| token prices | `token_prices_upsert` + `apply_prices` + `mark_price_misses` | Spot cache; apply hits; mark Dex/CG misses as known-unknown |
| token contracts discovery | `wallets.wallet_token_contracts_upsert(p_wallet_id, p_chain_id, p_rows jsonb)` | `wallets.wallet_token_contracts` (insert/update only; no delete) |
| token portfolio discovery | `wallets.wallet_token_positions_insert(p_wallet_id, p_chain_id, p_rows jsonb)` | `wallets.wallet_token_positions` (INSERT … ON CONFLICT DO NOTHING) |
| LP positions discovery | `wallets.wallet_lp_positions_upsert(p_wallet_id, p_chain_id, p_rows jsonb)` | `wallets.wallet_lp_positions` (DELETE+INSERT replace per wallet+chain; stamps `calculated_at`) |

CEX `p_rows` is a JSON array of Dune row objects (`blockchain`, `address`, `cex_name`, `distinct_name`). Empty array raises. Script: `gsa-supabase-schema/supabase/scripts/wallets_cex_addresses_upsert.sql`.

Token prices enrich upserts `{chain_id, contract_address, symbol?, price_usd?, source, liquidity_usd?}` (`source` = dexscreener|coingecko|miss). Upsert dedupes PK in SQL (`DISTINCT ON`). Platforms from `chains.subdomain_*`. After Dex+CG miss: `mark_price_misses` sets `has_price_error=false` and `quality_reason=unknown_token_dex_coingecko_defillama`. Scripts: `chains_price_subdomains.sql`, `wallets_token_prices_spot_cache.sql`, `wallet_token_positions_mark_price_misses.sql`.

Discovery `p_rows` is a JSON array of `{contract_address, source?}`. Empty array is a no-op (does not delete). Script: `gsa-supabase-schema/supabase/scripts/wallet_token_contracts_upsert_no_delete.sql`.

Portfolio positions `p_rows` include `contract_address` (`'native'` or `0x…`), amounts, `price_usd`, `has_price_error`, `token_quality` (`priced`|`unpriced`|`spam`), `quality_reason`, etc. Initial prices from DeFiLlama; Dex/CG fill via `token_prices_import`. Script: `gsa-supabase-schema/supabase/scripts/wallet_token_portfolio_discovery.sql`. Quality columns: `gsa-supabase-schema/supabase/scripts/wallet_token_positions_quality.sql`. Reset / re-queue: `wallet_token_portfolio_discovery_reset.sql` (TRUNCATE + re-flag; required because insert is DO NOTHING).

LP positions `p_rows` include `position_kind` (`nft`|`classic_lp`|`classic_staked`), pool/NFT keys, amounts, USD, `group_id`. Classic targets: `wallets.lp_pools`. Script: `wallet_lp_positions_discovery.sql`. Reset: `wallet_lp_positions_discovery_reset.sql` (ask before running).

## Triggers (schema repo)

When `is_valid_*` becomes true, DB triggers set the matching `next_eligible_at` to `-infinity`:

- `trg_wallet_daily_next_eligible_at`
- `trg_wallet_monthly_next_eligible_at`
- `trg_wallet_history_next_eligible_at`
- `trg_wallet_transactions_discovery_flag_bi` (sets `does_need_discovery_contracts` on insert from `chains.subdomain_alchemy`)
- `trg_wallet_transactions_portfolio_flag_bu` (sets `does_need_portfolio_discovery` when contract discovery completes)
- `trg_wallet_transactions_lp_flag_bu` (sets `does_need_lp_discovery` when portfolio discovery completes)

## Claim pattern

```sql
WITH candidates AS (
  SELECT w.id
  FROM erc_8004.wallets w
  WHERE <eligible>
  ORDER BY w.<next_eligible_at>, w.id
  LIMIT %(limit)s
  FOR UPDATE SKIP LOCKED
)
UPDATE erc_8004.wallets w
SET
  <status> = 'Pending',
  <next_eligible_at> = NOW() + make_interval(secs => %(stale_seconds)s),
  ...
FROM candidates c
WHERE w.id = c.id
RETURNING w.id, w.address
```

`FOR UPDATE SKIP LOCKED` lets daily `worker-a` / `worker-b` claim disjoint batches.

### After save (schedule next run)

| Worker | Next eligibility |
|---|---|
| daily | Midnight UTC of the **next calendar day** |
| monthly / origin | `NOW() + 30 days` |

## Chains / Alchemy

```sql
SELECT chain_id, subdomain_alchemy
FROM erc_8004.chains
WHERE is_active = TRUE
```

RPC order per chain: public endpoints (`networks.py`) → Alchemy batch (`alchemy.py`) using `subdomain_alchemy`.

Chains: ethereum, base, arbitrum, polygon, bsc, celo, gnosis, xlayer.

## Monitoring SQL

### Eligible now

```sql
-- daily
SELECT COUNT(*) FROM erc_8004.wallets
WHERE is_valid_import_current_nonce_and_balance_daily IS TRUE
  AND import_nonce_and_balance_daily_next_eligible_at <= NOW();

-- monthly
SELECT COUNT(*) FROM erc_8004.wallets
WHERE is_valid_import_current_nonce_and_balance_monthly IS TRUE
  AND import_nonce_and_balance_monthly_next_eligible_at <= NOW();

-- origin
SELECT COUNT(*) FROM erc_8004.wallets
WHERE is_valid_import_current_nonce_and_balance_monthly IS TRUE
  AND import_wallet_history_next_eligible_at <= NOW();

-- token contracts discovery
SELECT COUNT(*) FROM erc_8004.wallet_transactions wt
JOIN erc_8004.chains c ON c.id = wt.chain_id
WHERE wt.does_need_discovery_contracts IS DISTINCT FROM FALSE
  AND c.subdomain_alchemy IS NOT NULL
  AND btrim(c.subdomain_alchemy) <> ''
  AND (
    wt.discovery_contracts_claimed_at IS NULL
    OR wt.discovery_contracts_claimed_at < NOW() - interval '2 hours'
  );
```

### Stuck Completed (snapshot not applied)

```sql
-- daily
SELECT COUNT(*) FROM erc_8004.wallets
WHERE import_nonce_and_balance_daily_last_status = 'Completed'
  AND import_current_nonce_and_balance_daily_json IS NOT NULL
  AND import_current_nonce_and_balance_daily_json <> '{}'::jsonb;

-- monthly
SELECT COUNT(*) FROM erc_8004.wallets
WHERE import_nonce_and_balance_monthly_last_status = 'Completed'
  AND import_current_nonce_and_balance_monthly_json IS NOT NULL
  AND import_current_nonce_and_balance_monthly_json <> '{}'::jsonb;

-- origin
SELECT COUNT(*) FROM erc_8004.wallets
WHERE import_wallet_history_status = 'Completed'
  AND import_wallet_history_data IS NOT NULL
  AND import_wallet_history_data <> '{}'::jsonb;
```

### Backfill snapshot (batch)

```sql
SELECT erc_8004.wallet_apply_daily_snapshot(w.id)
FROM erc_8004.wallets w
WHERE w.import_nonce_and_balance_daily_last_status = 'Completed'
  AND w.import_current_nonce_and_balance_daily_json IS NOT NULL
  AND w.import_current_nonce_and_balance_daily_json <> '{}'::jsonb
ORDER BY w.id
LIMIT 50;
```

(Same pattern with `wallet_apply_monthly_snapshot` / `wallet_apply_owner_history_snapshot`.)

### Force re-queue Errors

```sql
UPDATE erc_8004.wallets
SET import_nonce_and_balance_daily_next_eligible_at = '-infinity'
WHERE is_valid_import_current_nonce_and_balance_daily IS TRUE
  AND import_nonce_and_balance_daily_last_status = 'Error';
```

(Adjust column names for monthly / origin.)

### CEX addresses (`wallets.cex_addresses`)

```sql
SELECT count(*) AS rows, max(updated_at) AS last_updated
FROM wallets.cex_addresses;

SELECT chain, count(*) AS n
FROM wallets.cex_addresses
GROUP BY 1
ORDER BY n DESC
LIMIT 10;
```

### Token prices (`wallets.token_prices`)

```sql
SELECT source, count(*), count(*) FILTER (WHERE price_usd IS NOT NULL) AS with_price
FROM wallets.token_prices
GROUP BY 1;

SELECT id, subdomain_coingecko, subdomain_dexscreener
FROM erc_8004.chains
ORDER BY id;
```

### Token contracts discovery

```sql
SELECT
  count(*) FILTER (WHERE does_need_discovery_contracts IS DISTINCT FROM FALSE) AS pending,
  count(*) FILTER (WHERE does_need_discovery_contracts = FALSE) AS attempted,
  count(*) FILTER (WHERE has_discovery_contracts_error IS TRUE) AS errors
FROM erc_8004.wallet_transactions;

SELECT c.name, count(*) AS pending
FROM erc_8004.wallet_transactions wt
JOIN erc_8004.chains c ON c.id = wt.chain_id
WHERE wt.does_need_discovery_contracts IS DISTINCT FROM FALSE
GROUP BY 1
ORDER BY pending DESC;

SELECT count(*) AS contracts, count(DISTINCT wallet_id) AS wallets
FROM wallets.wallet_token_contracts;

-- Re-queue failures
-- UPDATE erc_8004.wallet_transactions
-- SET does_need_discovery_contracts = TRUE,
--     has_discovery_contracts_error = NULL,
--     discovery_contracts_message_error = NULL,
--     discovery_contracts_claimed_at = NULL,
--     discovery_contracts_claimed_by = NULL
-- WHERE has_discovery_contracts_error IS TRUE;
```

### Token portfolio discovery

```sql
SELECT
  count(*) FILTER (WHERE does_need_portfolio_discovery IS DISTINCT FROM FALSE) AS pending,
  count(*) FILTER (WHERE has_portfolio_discovery_error IS TRUE) AS errors
FROM erc_8004.wallet_transactions;

SELECT count(*) AS positions,
       count(*) FILTER (WHERE contract_address = 'native') AS native_rows,
       count(*) FILTER (WHERE has_price_error IS TRUE) AS price_errors
FROM wallets.wallet_token_positions;

SELECT token_quality, quality_reason, count(*)
FROM wallets.wallet_token_positions
GROUP BY 1, 2
ORDER BY count(*) DESC;

-- Polygon native should be priced after POL key fix + reset re-run
SELECT chain_id,
       count(*) FILTER (WHERE has_price_error IS NOT TRUE) AS native_ok,
       count(*) FILTER (WHERE has_price_error IS TRUE) AS native_err
FROM wallets.wallet_token_positions
WHERE contract_address = 'native'
GROUP BY 1
ORDER BY 1;
```

**Full rediscovery** (after pricing/quality code changes): deploy schema + worker, then run `gsa-supabase-schema/supabase/scripts/wallet_token_portfolio_discovery_reset.sql`, then `workflow_dispatch` `wallet-token-portfolio-discovery`.

### LP positions discovery

```sql
SELECT
  count(*) FILTER (WHERE does_need_lp_discovery IS DISTINCT FROM FALSE) AS pending,
  count(*) FILTER (WHERE has_lp_discovery_error IS TRUE) AS errors
FROM erc_8004.wallet_transactions;

SELECT position_kind, protocol, count(*)
FROM wallets.wallet_lp_positions
GROUP BY 1, 2
ORDER BY count(*) DESC;

SELECT count(*) AS active_pools
FROM wallets.lp_pools
WHERE active IS TRUE;

-- Stale snapshots (inputs for future 15d refresh worker)
SELECT count(DISTINCT (wallet_id, chain_id)) AS stale_wallet_chains
FROM wallets.wallet_lp_positions
WHERE calculated_at < NOW() - interval '15 days';
```

**Full rediscovery** (ask before TRUNCATE): `wallet_lp_positions_discovery_reset.sql` then `workflow_dispatch` `wallet-lp-positions-discovery`.

## Related docs

- [ARCHITECTURE.md](./ARCHITECTURE.md) — GHA pipeline and state machine
- [OPS.md](./OPS.md) — stuck wallets, logs, when to touch schema
- Worker READMEs under `workers/*/README.md`
