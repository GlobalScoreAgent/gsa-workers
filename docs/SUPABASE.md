# Supabase / Postgres interaction

Workers connect with **direct Postgres** via `SUPABASE_DB_URL` (`psycopg`), not supabase-js or Edge Functions. Schema of truth for wallet claim jobs: `erc_8004`. Reference-data imports (CEX addresses, token prices) use schema `wallets`.

Schema migrations and snapshot/upsert SQL live in the sibling repo **`gsa-supabase-schema`** (functions `wallet_apply_*_snapshot`, `wallets.cex_addresses_upsert`, `wallets.token_prices_upsert`, triggers, indexes). Code of truth for claim/save SQL in this repo: each worker’s `src/db.py`.

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
| `wallets.token_prices` | Daily token prices (Dune import) |

## Per-worker column map

| Worker | Valid flag | Schedule column | Payload | Status column | Timestamp |
|---|---|---|---|---|---|
| **daily** | `is_valid_import_current_nonce_and_balance_daily` | `import_nonce_and_balance_daily_next_eligible_at` | `import_current_nonce_and_balance_daily_json` | `import_nonce_and_balance_daily_last_status` | `import_nonce_and_balance_daily_at` |
| **monthly** | `is_valid_import_current_nonce_and_balance_monthly` | `import_nonce_and_balance_monthly_next_eligible_at` | `import_current_nonce_and_balance_monthly_json` | `import_nonce_and_balance_monthly_last_status` | `import_nonce_and_balance_monthly_at` |
| **origin** | `is_valid_import_current_nonce_and_balance_monthly` | `import_wallet_history_next_eligible_at` | `import_wallet_history_data` | `import_wallet_history_status` | `import_wallet_history_at` |

Daily also uses claim metadata:

- `import_nonce_and_balance_daily_claimed_at`
- `import_nonce_and_balance_daily_claimed_by` (`WORKER_ID`)

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
| token prices | `wallets.token_prices_upsert(p_rows jsonb)` | `wallets.token_prices` (`ON CONFLICT (contract_address, blockchain, price_date) DO NOTHING`) |

CEX `p_rows` is a JSON array of Dune row objects (`blockchain`, `address`, `cex_name`, `distinct_name`). Empty array raises. Script: `gsa-supabase-schema/supabase/scripts/wallets_cex_addresses_upsert.sql`.

Token prices `p_rows` is a JSON array of Dune row objects (`symbol`, `blockchain`, `day`, `avg_price`, `contract_address`). Empty array raises. Script: `gsa-supabase-schema/supabase/scripts/wallets_token_prices_upsert.sql`.

## Triggers (schema repo)

When `is_valid_*` becomes true, DB triggers set the matching `next_eligible_at` to `-infinity`:

- `trg_wallet_daily_next_eligible_at`
- `trg_wallet_monthly_next_eligible_at`
- `trg_wallet_history_next_eligible_at`

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
SELECT count(*) AS rows, max(price_date) AS max_price_date
FROM wallets.token_prices;

SELECT blockchain, count(*) AS n
FROM wallets.token_prices
GROUP BY 1
ORDER BY n DESC
LIMIT 10;
```

## Related docs

- [ARCHITECTURE.md](./ARCHITECTURE.md) — GHA pipeline and state machine
- [OPS.md](./OPS.md) — stuck wallets, logs, when to touch schema
- Worker READMEs under `workers/*/README.md`
