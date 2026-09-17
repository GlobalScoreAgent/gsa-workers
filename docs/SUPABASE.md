# Supabase / Postgres interaction

Workers connect with **direct Postgres** via `SUPABASE_DB_URL` (`psycopg`), not supabase-js or Edge Functions. Schema of truth for wallet claim jobs: `erc_8004`. Reference-data imports (Dune queries, token prices) use schema `wallets`. ERC-8257 tools import uses schema `erc_8257`. URI ingest uses `erc_8004.uri_documents` + `erc_8004.agent_manifest`. AI agent classifier uses `web_dashboard.agents` + schema `llm` + `web_dashboard.agent_ai_categories`.

Schema migrations and snapshot/upsert SQL live in the sibling repo **`gsa-supabase-schema`** (functions `wallet_apply_*_snapshot`, Dune reference / token_prices / discovery upserts, URI indexes + helpers, triggers). Code of truth for claim/save SQL in this repo: each worker’s `src/db.py`. Process catalog: [PROCESSES.md](./PROCESSES.md). Live token/LP fill is **`wallet_holdings_discovery`** (the three split workers were deleted 2026-09-16). LP refresh (15d) still pending: [PENDING_LP_POSITIONS.md](./PENDING_LP_POSITIONS.md).

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
| `erc_8004.wallet_daily_metrics` | Daily flat nonce/balance per wallet×chain×date (written by daily snapshot). `snapshot_date` = Postgres `CURRENT_DATE` (DB timezone, typically UTC). |
| `erc_8004.wallet_transactions` | Read model: current nonce/balance + 30d history + category. Updated by **`wallet_rollup_daily_metrics`** (not by daily snapshot). Claim queue for token/LP discovery + **activity flows 15d** + **funding transfers**. |
| `erc_8004.chain_nonces` | Per-chain daily nonce totals (not written by current daily snapshot) |
| `erc_8004.wallet_owner_details` | Monthly + origin snapshots: owner metrics / first tx |
| `wallets.cex_addresses` | CEX address reference list (Dune import) |
| `wallets.mixer_addresses` | Mixer / Tornado pool addresses (Dune) |
| `wallets.bridge_addresses` | Bridge label addresses (Dune) |
| `wallets.ofac_sanction_addresses` | OFAC sanction addresses (Dune) |
| `wallets.token_prices` | Spot USD cache PK `(chain_id, contract)`; Dex/CG enrich |
| `wallets.wallet_token_contracts` | ERC-20 contracts with balance > 0 per wallet+chain (discovery) |
| `wallets.wallet_token_positions` | Fungible positions (native=`'native'` + ERC-20); initial INSERT discovery |
| `wallets.lp_pools` | Classic LP scan targets (`active` toggle); seeded Aerodrome V1 on Base |
| `wallets.wallet_lp_positions` | LP snapshots (UniV3 NFT + classic); PK `(wallet_id, chain_id, position_kind, nft_manager, token_id, pool)`; FKs to `wallets`/`chains`; `calculated_at` for future 15d refresh |
| `wallets.wallet_nft_contracts` | NFT collections touched by activity scan (`erc721`/`erc1155`) |
| `wallets.wallet_token_transfers` | Wallet-centric ERC-20/721(/1155) transfer rows from activity scan |
| `erc_8004.uri_documents` | Canonical resolved JSON by `uri_hash = md5(uri)` (UNIQUE); TTL `expires_at` (~15d write); `fetched_at` / `document` / `status` |
| `erc_8004.agent_manifest` | Envelope per agent/feedback (`uri_document_id` FK); `source`, revoke fields, `has_download_error`, `reprocess_count`, `is_processed` — **no** `data`/`url` columns |
| `erc_8004.agents` | Queue via `is_uri_processed` + `agent_uri_raw` |
| `erc_8004.registration_feedbacks` | Queue via `is_feedback_processed` (`feedback_on_chain` / URI / endpoint); `is_uri_processed` unused for this pipeline |
| `web_dashboard.agents` | Dashboard agent rows; AI classifier queue + results |
| `web_dashboard.agent_ai_categories` | Active taxonomy for AI classification |
| `llm.process` / `llm.llm_provider` / `llm.models` / `llm.procees_llm_providers` / `llm.models_requests` | LLM config + daily request counters |
| `erc_8257.tools` | ERC-8257 tool catalog mirror (agenttoolindex); PK `(chain_id, tool_id)`; FK `creator_wallet_id` |
| `erc_8257.sync_state` | Watermark `source_synced_at` for agenttoolindex sync short-circuit |
| `erc_8004.agent_metadata_services` | Parsed metadata services (`endpoint`, `internal_type`). Profile process DELETE+INSERT. |
| `erc_8004.agent_endpoint_health` | 15d HTTP census queue + last result. PK `(agent_id, endpoint_normalized)`. |
| `erc_8004.agent_endpoint_status` | View: per-agent `live` / `degraded` / `down` / `unknown` |
| `ethos.profiles` | Ethos identity + reviews API watermark (`reviews_fetched_at` / `reviews_next_eligible_at`) |
| `ethos.reviews` | Review rows for HUMI Ethos signals; filled by `ethos_reviews_api` (not Goldsky) |

## Per-worker column map

| Worker | Valid flag | Schedule column | Payload | Status column | Timestamp |
|---|---|---|---|---|---|
| **daily** | `is_valid_import_current_nonce_and_balance_daily` | `import_nonce_and_balance_daily_next_eligible_at` | `import_current_nonce_and_balance_daily_json` | `import_nonce_and_balance_daily_last_status` | `import_nonce_and_balance_daily_at` |
| **owner monthly**, lane `monthly` | `is_valid_import_current_nonce_and_balance_monthly` | `import_nonce_and_balance_monthly_next_eligible_at` | `import_current_nonce_and_balance_monthly_json` | `import_nonce_and_balance_monthly_last_status` | `import_nonce_and_balance_monthly_at` |
| **owner monthly**, lane `origin` | `is_valid_import_current_nonce_and_balance_monthly` | `import_wallet_history_next_eligible_at` | `import_wallet_history_data` | `import_wallet_history_status` | `import_wallet_history_at` |

Both owner lanes live in the single worker `owner_wallet_monthly` and each one only ever writes its own clock, payload and status. A wallet due on one clock is processed for that task alone. Rate-limited chains shorten the clock to `TRANSIENT_REQUEUE_SECONDS` (1h) instead of the usual 30 days and never set `Error`.

### Ethos reviews API (`ethos.profiles`)

| Column | Role |
|---|---|
| `reviews_fetched_at` | Last successful API fetch. `NULL` = full paginate |
| `reviews_next_eligible_at` | Due clock. `NULL` or `<= now()` = eligible. Success → `+1 day` |
| `reviews_claimed_at` / `reviews_claimed_by` | Soft lock (stale 2h) |
| `reviews_last_status` | `ok` \| `error` |

Universe: Claimed `profile_addresses.wallet_id IS NOT NULL`. RPCs `ethos.claim_reviews_fetch` / `complete_reviews_fetch`. Upsert destination `ethos.reviews` (`graph_id = ethos-api:review:{id}`).

Daily also uses claim metadata:

- `import_nonce_and_balance_daily_claimed_at`
- `import_nonce_and_balance_daily_claimed_by` (`WORKER_ID`)

### HUMI reason publisher (`index_humi.index_humi_agent`)

| Column | Role |
|---|---|
| `needs_reason_publish` | Queue flag. `agent_index_humi_calculate` raises it on every recalculation |
| `reason_published_at` | Last effective publish |
| `reason_content_sha256` | Hash of the last uploaded document; drives the skip-upload short-circuit |
| `reason_publish_claimed_at` / `reason_publish_claimed_by` | Soft lock (stale 2h), `humi_reason_publisher/gha:{WORKER_ID}` |

RPCs `index_humi.claim_reason_publish(limit, worker_id, stale_seconds)` / `complete_reason_publish(jsonb)`. Destination is **not** a table: private bucket `humi-reasons`, object `humi/agent/{agent_id}.json`.

`complete_reason_publish` takes the `version` captured at claim time and only clears the flag if it still matches. A recalculation mid-flight leaves the flag raised, so the stale object is republished next run instead of silently drifting.

`release_reason_publish(bigint[])` exists but the worker never calls it — see [PROCESSES.md](./PROCESSES.md#17-humi-reason-publisher).

### Agent series export (`erc_8004.agents`)

| Column | Role |
|---|---|
| `series_export_as_of` | Last exported cycle (T-1). **This is the queue**: `NULL` or below the target means pending. No boolean flag, on purpose |
| `series_export_at` | Last ack |
| `series_export_sha256` | Hash of the last uploaded object; auditing only, it cannot short-circuit a PUT because the window moves daily |
| `series_export_claimed_at` / `series_export_claimed_by` | Soft lock (stale 2h), `agent_series_export/gha:{WORKER_ID}` |

Index `idx_agents_series_export_claim (series_export_as_of NULLS FIRST, series_export_claimed_at NULLS FIRST, id)`: exported agents sort to the end, so the claim always finds pending rows at the front.

RPCs `erc_8004.agent_series_claim(batch, as_of, worker_id, stale_seconds)` → `(agent_id, document jsonb)`, `agent_series_ack(rows jsonb, as_of)`, `agent_series_release(bigint[])`, `agent_tx_scalars_refresh(as_of, batch, after_agent_id)` → `(agents_scanned, rows_upserted, last_agent_id)`, `agent_series_cycle_open/close(as_of)`.

Destinations: public bucket `agent-series` (`agents/{agent_id}.json`) and table `erc_8004.agent_tx_scalars` (`nonce`, deltas 7/15/30, `first_nonce`, `nonce_history_span_days`, `as_of`).

`document` comes back `NULL` for agents with no valid wallet or no `wallet_transactions` rows; they are acked without an upload so the cycle can close.

### Token contracts discovery (`wallet_transactions`)

Live consumer: **`wallet_holdings_discovery`** (stage 1). Split worker schedule is off.

| Column | Role |
|---|---|
| `does_need_discovery_contracts` | `NULL`/`true` = pending; `false` = attempted (success or error) |
| `discovery_contracts_claimed_at` | In-flight claim lock; after attempt kept as last-attempt timestamp (`NOW()`) |
| `discovery_contracts_claimed_by` | Audit id `wallet_holdings_discovery/gha:{WORKER_ID}` (legacy prefix `wallet_token_contracts_discovery/gha:`) |
| `has_discovery_contracts_error` | `TRUE` if last attempt failed |
| `discovery_contracts_message_error` | Last error text; `NULL` on success |

Eligibility: flag pending **and** `chains.subdomain_alchemy` non-empty. New `wallet_transactions` inserts get the flag from trigger `trg_wallet_transactions_discovery_flag_bi`. On process error the worker sets flag `FALSE` and fills the error columns so the queue does not re-claim the same row forever. **HTTP 429 / timeouts / 5xx are not this path** — `wallet_holdings_discovery` leaves the flag pending (`release_transient`).

### Token portfolio discovery (`wallet_transactions`)

Live consumer: **`wallet_holdings_discovery`** (stage 2, same run after contracts OK).

| Column | Role |
|---|---|
| `does_need_portfolio_discovery` | Pending after contract discovery done |
| `portfolio_discovery_claimed_at` | Claim lock / last attempt |
| `portfolio_discovery_claimed_by` | Audit id `wallet_holdings_discovery/gha:{WORKER_ID}` (legacy prefix `wallet_token_portfolio_discovery/gha:`) |
| `has_portfolio_discovery_error` | Last attempt failed |
| `portfolio_discovery_message_error` | Error text |

Trigger `trg_wallet_transactions_portfolio_flag_bu` sets portfolio pending when contract discovery completes successfully.

### LP positions discovery (`wallet_transactions`)

Live consumer: **`wallet_holdings_discovery`** (stage 3, same run after portfolio OK).

| Column | Role |
|---|---|
| `does_need_lp_discovery` | Pending after portfolio discovery done |
| `lp_discovery_claimed_at` | Claim lock / last attempt |
| `lp_discovery_claimed_by` | Audit id `wallet_holdings_discovery/gha:{WORKER_ID}` (legacy prefix `wallet_lp_positions_discovery/gha:`) |
| `has_lp_discovery_error` | Last attempt failed |
| `lp_discovery_message_error` | Error text |

Trigger `trg_wallet_transactions_lp_flag_bu` sets LP pending when portfolio discovery completes successfully.

### Activity flows 15d (`wallet_transactions` claim → staging)

| Column | Role |
|---|---|
| `is_valid_activity_flows` | Chain is in the 15d map (ETH, Arb, Polygon, Celo, Base, Gnosis, BSC, X Layer) |
| `activity_flows_agent_ok` | Denormalized valid-agent gate (`agent_wallet_tx` × `agents.valid`). Triggers keep it fresh; claim uses this instead of EXISTS |
| `activity_flows_next_eligible_at` | Claim clock. Success → next UTC cut (day 15 00:00, or day 1 next month). New inserts `-infinity` via BI |
| `activity_flows_claimed_at` / `claimed_by` | Soft lock (`CLAIM_STALE_SECONDS`) |
| `activity_flows_completed_at` | Last successful ingest (empty window still counts) |
| `has_activity_flows_error` / `activity_flows_message_error` | Last failure (requeue +1h) |

Eligibility: `is_valid_activity_flows` + `activity_flows_agent_ok` + due clock + `wallet_category NOT LIKE 'Dormant_%'` + `lower(wallets.address) <> 0x0` (null address excluded — Alchemy transfer pagination OOMs the runner). Index: `idx_wallet_transactions_activity_flows_claim`. No FIFO `ORDER BY` on claim (full-set 15d drain). Schedule: UTC window **18:00→12:00** (closed 12–18).

```sql
SELECT
  count(*) FILTER (WHERE is_valid_activity_flows IS TRUE) AS seeded,
  count(*) FILTER (
    WHERE is_valid_activity_flows IS TRUE
      AND COALESCE(wallet_category, '') LIKE 'Dormant_%'
  ) AS dormant,
  count(*) FILTER (
    WHERE is_valid_activity_flows IS TRUE
      AND COALESCE(wallet_category, '') NOT LIKE 'Dormant_%'
  ) AS non_dormant,
  count(*) FILTER (
    WHERE is_valid_activity_flows IS TRUE
      AND activity_flows_agent_ok IS TRUE
      AND activity_flows_next_eligible_at IS NOT NULL
      AND activity_flows_next_eligible_at <= NOW()
      AND COALESCE(wallet_category, '') NOT LIKE 'Dormant_%'
  ) AS due_now,
  count(*) FILTER (WHERE has_activity_flows_error IS TRUE) AS errors,
  count(*) FILTER (WHERE activity_flows_completed_at IS NOT NULL) AS completed
FROM erc_8004.wallet_transactions;
```

`dormant` rises and `non_dormant` falls as `wallet_tx_rollup` reclassifies — expected. Baseline 2026-08-13: seeded 294 948 / dormant 77 159 / non_dormant 217 789. Same snapshot is required in skill `gsa-worker-health`. Through 2026-08-31 UTC leftover BSC due rows drain via Alchemy key_2 (Ankr Freemium exhausted 2026-08-24).

Staging table: `wallets.wallet_activity_transfers` via `wallets.wallet_activity_transfers_insert`. PK `(wallet_id, chain_id, unique_id)`. `chain_id` is `erc_8004.chains.id`. Migrations: `20260813010000_wallet_activity_transfers.sql`, `20260906060059_activity_flows_agent_ok_claim.sql`. Schema doc: `gsa-supabase-schema/supabase/docs/wallet-activity-transfers.md`.

### Funding transfers (first inflows)

| Column | Role |
|---|---|
| `is_valid_funding_transfers` | Chain in funding map (same 8 EVM ids as activity) |
| `funding_transfers_next_eligible_at` | Claim clock. Success → `infinity`. New inserts `-infinity` via BI |
| `funding_transfers_claimed_at` / `claimed_by` | Soft lock (`CLAIM_STALE_SECONDS`) |
| `funding_transfers_completed_at` | Last successful ingest (empty still counts) |
| `has_funding_transfers_error` / `funding_transfers_message_error` | Last failure (requeue +1h) |

Claim prefers `wallet_category NOT LIKE 'Dormant_%'`. Quota exhausted unlocks the in-flight batch.

```sql
SELECT
  count(*) FILTER (WHERE is_valid_funding_transfers IS TRUE) AS seeded,
  count(*) FILTER (
    WHERE is_valid_funding_transfers IS TRUE
      AND COALESCE(wallet_category, '') NOT LIKE 'Dormant_%'
      AND funding_transfers_next_eligible_at <= NOW()
  ) AS due_active,
  count(*) FILTER (WHERE funding_transfers_completed_at IS NOT NULL) AS completed
FROM erc_8004.wallet_transactions;
```

Table: `wallets.wallet_funding_transfers` via `wallets.wallet_funding_transfers_insert`. Migration: `20260826010000_wallet_funding_transfers.sql`.

Probe/enrich census columns were dropped. Do not revive them ([DEPRECATION.md](./DEPRECATION.md)).

### URI ingest (`uri_documents` / `agent_manifest`)

| Column / object | Role |
|---|---|
| `uri_documents.uri` | Canonical URI string (may be long; uniqueness is on hash) |
| `uri_documents.uri_hash` | `md5(uri)` UNIQUE lookup key for upsert |
| `uri_documents.document` | Resolved JSON payload |
| `uri_documents.fetched_at` / `expires_at` | Refresh clock (last **attempt**, including failed fetches); reprocess claims off-chain when `fetched_at` &gt; 15d |
| `uri_documents.status` | e.g. `valid` for refresh eligibility |
| `agent_manifest.uri_document_id` | FK to canonical doc |
| `agent_manifest.provider` / ids | Link back to `agents` or `registration_feedbacks` to recover URI (no `url` column) |
| `agent_manifest.has_download_error` / `reprocess_count` | Error queue (max 3 retries; first immediate; later need `updated_at` &gt; 3d ago) |
| `agent_manifest.does_need_manual_reprocess` | Force into error reprocess path |
| `agent_manifest.is_processed` | Manifest consume flag; set `false` after successful error fix or **changed** refresh |
| `agents.is_uri_processed` | `false` = pending resolve |
| `registration_feedbacks.is_feedback_processed` | `false` = pending on-chain or external resolve |

Partial indexes (schema migrations `00065`–`00069`): `idx_agents_pending_uri_processing`, `idx_rf_pending_uri_resolve`, `idx_rf_pending_on_chain`, `idx_am_pending_reprocess`, `idx_ud_pending_refresh_offchain`. Claim predicates use `= false` (not `IS DISTINCT FROM TRUE`) so indexes hit.

Synthetic on-chain URI: `internal_on_chain_id_{feedback_id}`, `source='on_chain'` — no HTTP.

### AI agent classifier (`web_dashboard.agents` + `llm`)

| Column / object | Role |
|---|---|
| `does_need_ai_category_process` | `TRUE` = pending (set by another process; default true on new cols) |
| `ai_category_primary` / `ai_category_secondary` (json) | Classification result |
| `ai_category_confidence` / `ai_category_reasoning` / `ai_category_purpose` | Model output fields |
| `llm_model_id` | FK → `llm.models.id` used for this run |
| `ai_category_process_calculated_at` | Success or error timestamp |
| `has_ai_category_process_error` / `ai_category_process_error_message` | Error path (flag still cleared to `FALSE`) |
| `ai_category_input_hash` | MD5 of exact prompt inputs; used to copy classification and skip LLM |
| `llm.llm_provider.secret` | GitHub/env secret **name** (e.g. `GROQ`, `CLOUDFLARE`) |
| `llm.llm_provider.base_url` | OpenAI-compat API root (e.g. Groq `https://api.groq.com/openai/v1`; Cloudflare `…/accounts/{id}/ai/v1` + header `cf-aig-gateway-id`) |
| `llm.process.system_prompt` | Classifier system prompt (loaded by worker; edit in DB to refine) |
| `llm.models.request_per_day` / `request_per_minute` | Rate limits (requests); NVIDIA ids 15/19/20 use RPM **30** in DB (account-wide cap; worker also enforces `NVIDIA_ACCOUNT_RPM`) |
| `llm.models.tokens_per_minute` / `tokents_per_day` | Rate limits (tokens; note `tokents_per_day` spelling) |
| `llm.models_requests` | Daily counters PK uniqueness `(model_id, date)`; `request_total` + `token_total` (not incremented on HTTP 429) |
| `llm.procees_llm_providers` | Links `process_code='agent-classifier'` → providers |

Partial index: `idx_agents_pending_ai_category` (`WHERE does_need_ai_category_process IS TRUE`).
Partial error index: `idx_agents_ai_category_process_error` (`WHERE has_ai_category_process_error IS TRUE`) — lazy requeue batches (≤1000).
Partial donor index: `idx_agents_ai_category_input_hash_donors` (`ai_category_input_hash` where classified OK).

Backfill hashes after column deploy (same fingerprint as worker):

```bash
cd workers/ai_agent_classifier
uv run python backfill_input_hash.py
```

Script / migration capture: `agents_ai_category_input_hash.sql`.

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
| daily | `erc_8004.wallet_apply_daily_snapshot(p_wallet_id)` | `wallet_daily_metrics` (flat); status → `Processed`. Does **not** write `wallet_transactions` directly |
| rollup | `erc_8004.wallet_rollup_daily_metrics(p_batch_size)` | Rebuilds `wallet_transactions` from metrics |
| owner monthly, lane `monthly` | `erc_8004.wallet_apply_monthly_snapshot(p_wallet_id)` | `wallet_owner_details` (nonce/balance/type); status → `Processed` |
| owner monthly, lane `origin` | `erc_8004.wallet_apply_owner_history_snapshot(p_wallet_id)` | `wallet_owner_details.first_transaction_at`; status → `Processed` |

Canonical SQL / migrations: `gsa-supabase-schema/supabase/migrations/` and `supabase/scripts/wallet_apply_*.sql`.

**Do not** re-enable the old pg_cron jobs that used to do this work (see [DEPRECATION.md](./DEPRECATION.md)).

## Reference-data RPCs

| Worker | Function | Writes |
|---|---|---|
| dune queries | `wallets.cex_addresses_upsert(p_rows jsonb)` | `wallets.cex_addresses` |
| dune queries | `wallets.mixer_addresses_upsert(p_rows jsonb)` | `wallets.mixer_addresses` |
| dune queries | `wallets.bridge_addresses_upsert(p_rows jsonb)` | `wallets.bridge_addresses` |
| dune queries | `wallets.ofac_sanction_addresses_upsert(p_rows jsonb)` | `wallets.ofac_sanction_addresses` |
| token prices | `token_prices_upsert` + `apply_prices` + `mark_price_misses` | Spot cache; apply hits; mark Dex/CG misses as known-unknown |
| token contracts discovery | `wallets.wallet_token_contracts_upsert(p_wallet_id, p_chain_id, p_rows jsonb)` | `wallets.wallet_token_contracts` (insert/update only; no delete) |
| token portfolio discovery | `wallets.wallet_token_positions_insert(p_wallet_id, p_chain_id, p_rows jsonb)` | `wallets.wallet_token_positions` (INSERT … ON CONFLICT DO NOTHING) |
| LP positions discovery | `wallets.wallet_lp_positions_upsert(p_wallet_id, p_chain_id, p_rows jsonb)` | `wallets.wallet_lp_positions` (DELETE+INSERT replace per wallet+chain; stamps `calculated_at`) |
| activity flows 15d | `wallets.wallet_activity_transfers_insert(p_rows jsonb)` | Staging `wallets.wallet_activity_transfers` (INSERT … ON CONFLICT DO NOTHING) |
| funding transfers | `wallets.wallet_funding_transfers_insert(p_rows jsonb)` | Staging `wallets.wallet_funding_transfers` (INSERT … ON CONFLICT DO NOTHING) |
| endpoint liveness 15d | `agent_endpoint_health_sync` / `_claim` / `_complete` / `_complete_batch` | `erc_8004.agent_endpoint_health` |

Dune upserts: JSON arrays; empty array raises. Worker sends **chunks** (default 5000). Scripts: `wallets_cex_addresses_upsert.sql`, `wallets_dune_reference_tables.sql`. Docs: `gsa-supabase-schema/supabase/docs/wallets-dune-reference-tables.md`.

Token prices enrich upserts `{chain_id, contract_address, symbol?, price_usd?, source, liquidity_usd?}` (`source` = dexscreener|coingecko|miss). Upsert dedupes PK in SQL (`DISTINCT ON`). Platforms from `chains.subdomain_*`. After Dex+CG miss: `mark_price_misses` sets `has_price_error=false` and `quality_reason=unknown_token_dex_coingecko_defillama`. Scripts: `chains_price_subdomains.sql`, `wallets_token_prices_spot_cache.sql`, `wallet_token_positions_mark_price_misses.sql`.

Discovery `p_rows` is a JSON array of `{contract_address, source?}`. Empty array is a no-op (does not delete). Script: `gsa-supabase-schema/supabase/scripts/wallet_token_contracts_upsert_no_delete.sql`.

Portfolio positions `p_rows` include `contract_address` (`'native'` or `0x…`), amounts, `price_usd`, `has_price_error`, `token_quality` (`priced`|`unpriced`|`spam`), `quality_reason`, etc. Initial prices from DeFiLlama; Dex/CG fill via `token_prices_import`. Script: `gsa-supabase-schema/supabase/scripts/wallet_token_portfolio_discovery.sql`. Quality columns: `gsa-supabase-schema/supabase/scripts/wallet_token_positions_quality.sql`. Reset / re-queue: `wallet_token_portfolio_discovery_reset.sql` (TRUNCATE + re-flag; required because insert is DO NOTHING).

LP positions `p_rows` include `position_kind` (`nft`|`classic_lp`|`classic_staked`), pool/NFT keys, amounts, USD, `group_id`. Classic targets: `wallets.lp_pools`. Classic PK sentinels: `nft_manager_address=''`, `token_id=-1`. Scripts: `wallet_lp_positions_discovery.sql`, `wallet_lp_positions_pk_fk.sql`. Reset: `wallet_lp_positions_discovery_reset.sql` (ask before running). Schema docs: `gsa-supabase-schema/supabase/docs/wallet-lp-positions-discovery.md`.

### Progress vs LP row count

Most claimed wallets finish with **zero** LP rows (no NFT / no classic balance, or chain without extractor coverage). Prefer monitoring **attempted / pending / errors**, not only `count(*)` on `wallet_lp_positions`:

```sql
SELECT
  count(*) FILTER (WHERE does_need_lp_discovery IS DISTINCT FROM FALSE) AS pending,
  count(*) FILTER (WHERE does_need_lp_discovery IS FALSE
                   AND COALESCE(has_lp_discovery_error, FALSE) IS NOT TRUE) AS done_ok,
  count(*) FILTER (WHERE has_lp_discovery_error IS TRUE) AS errors,
  count(*) FILTER (WHERE lp_discovery_claimed_at IS NOT NULL
                   AND does_need_lp_discovery IS DISTINCT FROM FALSE) AS in_flight
FROM erc_8004.wallet_transactions;
```

## Triggers (schema repo)

When `is_valid_*` becomes true, DB triggers set the matching `next_eligible_at` to `-infinity`:

- `trg_wallet_daily_next_eligible_at`
- `trg_wallet_monthly_next_eligible_at`
- `trg_wallet_history_next_eligible_at`
- `trg_wallet_transactions_discovery_flag_bi` (sets `does_need_discovery_contracts` on insert from `chains.subdomain_alchemy`)
- `trg_wallet_transactions_portfolio_flag_bu` (sets `does_need_portfolio_discovery` when contract discovery completes)
- `trg_wallet_transactions_lp_flag_bu` (sets `does_need_lp_discovery` when portfolio discovery completes)
- `trg_wallet_transactions_activity_flows_bi` (sets `is_valid_activity_flows` + `-infinity` clock on insert for mapped EVM chains)
- `trg_awt_refresh_activity_flows_agent_ok` / `trg_agents_refresh_activity_flows_agent_ok` (keep `activity_flows_agent_ok` in sync)

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
| owner monthly (both lanes) | `NOW() + 30 days`, or `NOW() + TRANSIENT_REQUEUE_SECONDS` (1h) if any chain was rate-limited |

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

(Adjust column names for the owner monthly / origin lanes.)

### Dune reference tables

```sql
SELECT 'cex' AS src, count(*) AS rows, max(updated_at) AS last_updated FROM wallets.cex_addresses
UNION ALL
SELECT 'mixers', count(*), max(updated_at) FROM wallets.mixer_addresses
UNION ALL
SELECT 'bridges', count(*), max(updated_at) FROM wallets.bridge_addresses
UNION ALL
SELECT 'ofac', count(*), max(updated_at) FROM wallets.ofac_sanction_addresses;
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

### Holdings discovery (unified, live)

```sql
SELECT
  count(*) FILTER (WHERE does_need_discovery_contracts IS DISTINCT FROM FALSE) AS contracts_pending,
  count(*) FILTER (WHERE does_need_portfolio_discovery IS DISTINCT FROM FALSE) AS portfolio_pending,
  count(*) FILTER (WHERE does_need_lp_discovery IS DISTINCT FROM FALSE) AS lp_pending,
  count(*) FILTER (
    WHERE has_discovery_contracts_error IS TRUE
      AND discovery_contracts_message_error ~* '(429|too many requests|rate.?limit)'
  ) AS contracts_429,
  count(*) FILTER (
    WHERE has_portfolio_discovery_error IS TRUE
      AND portfolio_discovery_message_error ~* '(429|too many requests|rate.?limit)'
  ) AS portfolio_429,
  count(*) FILTER (
    WHERE has_lp_discovery_error IS TRUE
      AND lp_discovery_message_error ~* '(429|too many requests|rate.?limit)'
  ) AS lp_429
FROM erc_8004.wallet_transactions;
```

429-only requeue (prod **after** unified worker is live and split crons are off): sibling `supabase/scripts/wallet_discovery_reset_alchemy_429.sql`. Do not TRUNCATE positions/LP for this.

**Eligible now** — what the claim can actually take, across the three stages. Flag counts alone hide upstream blocks (`has_*_error` on the previous stage) and chains without `subdomain_alchemy`; a high pending with `eligible_now = 0` means stalled, not drained:

```sql
SELECT
  count(*) AS eligible_now_rows,
  count(DISTINCT wt.wallet_id) AS eligible_now_wallets
FROM erc_8004.wallet_transactions wt
JOIN erc_8004.chains c ON c.id = wt.chain_id
WHERE c.subdomain_alchemy IS NOT NULL
  AND btrim(c.subdomain_alchemy) <> ''
  AND (
    wt.does_need_discovery_contracts IS DISTINCT FROM FALSE
    OR (
      wt.does_need_portfolio_discovery IS DISTINCT FROM FALSE
      AND wt.does_need_discovery_contracts = FALSE
      AND COALESCE(wt.has_discovery_contracts_error, FALSE) IS NOT TRUE
    )
    OR (
      wt.does_need_lp_discovery IS DISTINCT FROM FALSE
      AND wt.does_need_portfolio_discovery = FALSE
      AND COALESCE(wt.has_portfolio_discovery_error, FALSE) IS NOT TRUE
    )
  );
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

**Full rediscovery** (after pricing/quality code changes): deploy schema + worker, then run `gsa-supabase-schema/supabase/scripts/wallet_token_portfolio_discovery_reset.sql`, then `workflow_dispatch` `wallet-holdings-discovery`.

### LP positions discovery

```sql
SELECT
  count(*) FILTER (WHERE does_need_lp_discovery IS DISTINCT FROM FALSE) AS pending,
  count(*) FILTER (WHERE does_need_lp_discovery IS FALSE
                   AND COALESCE(has_lp_discovery_error, FALSE) IS NOT TRUE) AS done_ok,
  count(*) FILTER (WHERE has_lp_discovery_error IS TRUE) AS errors
FROM erc_8004.wallet_transactions;

SELECT position_kind, protocol, count(*)
FROM wallets.wallet_lp_positions
GROUP BY 1, 2
ORDER BY count(*) DESC;

-- Done volume by chain (many chains have no LP extractor → 0 rows is normal)
SELECT wt.chain_id, c.name,
       count(*) AS wallets_done,
       count(*) FILTER (WHERE EXISTS (
         SELECT 1 FROM wallets.wallet_lp_positions p
         WHERE p.wallet_id = wt.wallet_id AND p.chain_id = wt.chain_id
       )) AS wallets_with_lp
FROM erc_8004.wallet_transactions wt
JOIN erc_8004.chains c ON c.id = wt.chain_id
WHERE wt.does_need_lp_discovery IS FALSE
  AND COALESCE(wt.has_lp_discovery_error, FALSE) IS NOT TRUE
GROUP BY 1, 2
ORDER BY wallets_done DESC;

SELECT count(*) AS active_pools
FROM wallets.lp_pools
WHERE active IS TRUE;

-- Stale snapshots (inputs for future 15d refresh worker)
SELECT count(DISTINCT (wallet_id, chain_id)) AS stale_wallet_chains
FROM wallets.wallet_lp_positions
WHERE calculated_at < NOW() - interval '15 days';
```

**Full rediscovery** (ask before TRUNCATE): `wallet_lp_positions_discovery_reset.sql` then `workflow_dispatch` `wallet-holdings-discovery`.

### Agent URI resolve (pending queues)

Claim predicates match worker SQL (`is_*_processed = false`). Prefer monitoring these counts over raw table size:

```sql
-- Agents pending first ingest
SELECT count(*) AS agents_pending
FROM erc_8004.agents
WHERE is_uri_processed = false
  AND agent_uri_raw IS NOT NULL
  AND agent_uri_raw <> '';

-- On-chain feedbacks (DB materialize, no HTTP)
SELECT count(*) AS on_chain_pending
FROM erc_8004.registration_feedbacks
WHERE is_feedback_processed = false
  AND feedback_type = 'feedback_on_chain'
  AND agent_id IS NOT NULL;

-- External feedback URI / endpoint
SELECT count(*) AS external_feedbacks_pending
FROM erc_8004.registration_feedbacks
WHERE is_feedback_processed = false
  AND feedback_type IN ('feedback_uri', 'feedback_end_point')
  AND agent_id IS NOT NULL;

SELECT count(*) AS uri_documents, count(*) FILTER (WHERE status = 'valid') AS valid_docs
FROM erc_8004.uri_documents;

SELECT source, count(*) AS manifests,
       count(*) FILTER (WHERE has_download_error IS TRUE) AS with_dl_error
FROM erc_8004.agent_manifest
GROUP BY 1
ORDER BY manifests DESC;
```

### Agent URI reprocess (errors + off-chain refresh)

```sql
-- Download errors eligible (matches CLAIM_ERROR_MANIFESTS_SQL)
SELECT count(*) AS errors_eligible
FROM erc_8004.agent_manifest
WHERE
  (has_download_error = true AND reprocess_count IS NULL)
  OR does_need_manual_reprocess = TRUE
  OR (
    has_download_error = true
    AND reprocess_count IS NOT NULL
    AND reprocess_count < 3
    AND updated_at < NOW() - interval '3 days'
  );

-- Off-chain docs older than 15 days (HTTP/IPFS only; excludes synthetic on-chain)
SELECT count(*) AS refresh_offchain_eligible
FROM erc_8004.uri_documents
WHERE status = 'valid'
  AND fetched_at < NOW() - interval '15 days'
  AND uri ~* '^(https?://|ipfs://)'
  AND NOT starts_with(uri, 'internal_on_chain_id_');

SELECT COALESCE(reprocess_count, 0) AS n, count(*)
FROM erc_8004.agent_manifest
WHERE has_download_error IS TRUE
GROUP BY 1
ORDER BY 1;

-- Recent failed refresh attempts (JSON kept; clock advanced)
SELECT count(*) AS refresh_error_stamped
FROM erc_8004.uri_documents
WHERE source_gateway LIKE 'refresh_error:%';
```

Re-run: **Actions** → `agent-uri-resolve` or `agent-uri-reprocess` → **Run workflow**. Worker READMEs: [`agent_uri_resolve`](../workers/agent_uri_resolve/README.md), [`agent_uri_reprocess`](../workers/agent_uri_reprocess/README.md).

### AI agent classifier

```sql
SELECT
  count(*) FILTER (WHERE does_need_ai_category_process IS TRUE) AS pending,
  count(*) FILTER (WHERE has_ai_category_process_error IS TRUE) AS errors,
  count(*) FILTER (WHERE ai_category_primary IS NOT NULL) AS classified
FROM web_dashboard.agents;

SELECT category_name
FROM web_dashboard.agent_ai_categories
WHERE is_active IS TRUE
ORDER BY id;

SELECT m.name, m.slug, mr.date, mr.request_total, m.request_per_day
FROM llm.models_requests mr
JOIN llm.models m ON m.id = mr.model_id
WHERE mr.date = CURRENT_DATE
ORDER BY m.id;

-- Lazy requeue errors (worker does this when clean claim queue is empty; batch ≤1000)
-- Script: gsa-supabase-schema/supabase/scripts/requeue_ai_category_process_errors.sql
-- WITH picked AS (
--   SELECT id FROM web_dashboard.agents
--   WHERE has_ai_category_process_error IS TRUE
--   ORDER BY id LIMIT 1000 FOR UPDATE SKIP LOCKED
-- )
-- UPDATE web_dashboard.agents a
-- SET does_need_ai_category_process = TRUE,
--     has_ai_category_process_error = NULL,
--     ai_category_process_error_message = NULL
-- FROM picked WHERE a.id = picked.id;

-- After changing classifier system prompt / taxonomy: wipe results + hashes and requeue
-- (script: gsa-supabase-schema/supabase/scripts/reset_ai_category_for_prompt_refresh.sql)
-- Clears ai_category_*, ai_category_input_hash, llm_model_id, calculated_at, errors;
-- sets does_need_ai_category_process = TRUE so copies do not reuse stale classifications.

-- Scoped requeue after Trading Bots / Invalid Metadata taxonomy refresh:
-- gsa-supabase-schema/supabase/scripts/requeue_ai_category_trading_niche_taxonomy.sql
-- Resets Trading + Other/Niche + null-primary error rows (clears hash).

-- Scoped requeue for hodlclaw/openclaw templates → Trading Bots:
-- gsa-supabase-schema/supabase/scripts/requeue_ai_category_hodlclaw_trading_bots.sql

-- New active categories (also): Invalid Metadata, Insufficient Metadata, Trading Bots
-- Prompt rules: llm.process.system_prompt (process_code=agent-classifier)
-- Scripts: agent_ai_categories_metadata_trading_bots.sql, llm_agent_classifier_system_prompt.sql
```

Re-run: **Actions** → `ai-agent-classifier` → **Run workflow**. README: [`ai_agent_classifier`](../workers/ai_agent_classifier/README.md).

## Monitoring — on-demand backfill (#13)

One GHA workflow (`on-demand-backfill.yml`) runs **four** sequential steps. Empty queue → skip; step error → continue.

```sql
SELECT jsonb_build_object(
  'ethos_history_pending', (
    SELECT count(*) FROM ethos.profile_addresses WHERE needs_history_fetch = true
  ),
  'ethos_score_due', (
    SELECT count(*) FROM ethos.list_score_candidates(100000)
  ),
  'official_scores', (SELECT count(*) FROM ethos.official_scores),
  'erc8183_pending', (
    SELECT count(*) FROM bsc_erc_8183.jobs WHERE needs_satellite_backfill = true
  ),
  'vacp_pending', (
    SELECT count(*) FILTER (WHERE needs_satellite_backfill) FROM virtual_acp.jobs
  ),
  'olas_pending', (
    SELECT count(*) FILTER (WHERE needs_satellite_backfill) FROM olas_mech.mechs
  ),
  'olas_requests', (SELECT count(*) FROM olas_mech.requests),
  'olas_deliveries', (SELECT count(*) FROM olas_mech.deliveries)
) AS on_demand_queues;
```

| Step | Queue | Source GraphQL |
|------|-------|----------------|
| `ethos_history` | `needs_history_fetch` | Goldsky slim (no reviews) |
| `ethos_reviews_api` | `reviews_next_eligible_at` | Ethos API v2 activities |
| `ethos_scores` | `list_score_candidates` | Ethos API (no subgraph) |
| `erc8183_satellites` | `bsc_erc_8183.jobs.needs_satellite_backfill` | Goldsky ERC-8183 BSC |
| `virtual_acp_satellites` | `virtual_acp.jobs.needs_satellite_backfill` | Goldsky Virtual ACP Base |
| `olas_mech_satellites` | `olas_mech.mechs.needs_satellite_backfill` | Autonolas Base + Gnosis |

## Monitoring — ERC-8257 tools import (#14)

**Live.** Snapshot 2026-08-15: 622 tools · 408 active Base+Eth · 207 linked · 58 creators · 304 agents with owner publisher.

```sql
SELECT * FROM erc_8257.sync_state;

SELECT chain_id, chain_name, status, count(*)
FROM erc_8257.tools
GROUP BY 1, 2, 3
ORDER BY 1, 3;

SELECT
  count(*) AS tools,
  count(*) FILTER (WHERE creator_wallet_id IS NOT NULL) AS linked,
  count(DISTINCT creator) FILTER (WHERE creator_wallet_id IS NOT NULL) AS creators_in_gsa
FROM erc_8257.tools
WHERE chain_id IN (1, 8453)
  AND status = 'active';
```

### Endpoint liveness 15d

```sql
SELECT erc_8004.agent_endpoint_health_sync();

SELECT
  count(*) FILTER (WHERE is_active) AS active,
  count(*) FILTER (
    WHERE is_active AND next_eligible_at <= now()
      AND (claimed_at IS NULL OR claimed_at < now() - interval '2 hours')
  ) AS due,
  count(*) FILTER (WHERE is_active AND is_reachable IS TRUE) AS reachable
FROM erc_8004.agent_endpoint_health;

SELECT status, count(*) FROM erc_8004.agent_endpoint_status GROUP BY 1;
```

Schema: sibling `gsa-supabase-schema` → `supabase/docs/agent-endpoint-health.md`.

## Monitoring — Ethos reviews API (#16)

```sql
SELECT
  count(*) FILTER (
    WHERE reviews_next_eligible_at IS NULL OR reviews_next_eligible_at <= now()
  ) AS due_clock,
  count(*) FILTER (WHERE reviews_fetched_at IS NOT NULL) AS fetched,
  count(*) FILTER (WHERE reviews_last_status = 'error') AS errors
FROM ethos.profiles p
WHERE EXISTS (
  SELECT 1 FROM ethos.profile_addresses pa
  WHERE pa.profile_id = p.profile_id
    AND pa.wallet_id IS NOT NULL
    AND lower(pa.status) = 'claimed'
);

SELECT count(*) FROM ethos.reviews;
```

Schema: sibling `gsa-supabase-schema` → `supabase/docs/ethos-reviews-api.md`.

## Monitoring — HUMI reason publisher (#17)

```sql
-- Queue and throughput
SELECT
  count(*) FILTER (WHERE needs_reason_publish)                       AS pending,
  count(*) FILTER (WHERE reason_published_at IS NOT NULL)            AS published,
  count(*) FILTER (WHERE reason_publish_claimed_at IS NOT NULL)      AS in_flight,
  count(*) FILTER (WHERE reason_published_at > now() - interval '1 minute') AS per_minute
FROM index_humi.index_humi_agent;

-- Objects must track `published`. A persistent gap means uploads returned 200
-- but the matching `complete` never reached the DB.
SELECT count(*), pg_size_pretty(sum((metadata->>'size')::bigint))
FROM storage.objects WHERE bucket_id = 'humi-reasons';

-- Narrative lag against the score. Up to ~6h is the 0/6/12/18 cron, not an incident.
SELECT count(*) AS behind, max(calculated_at - reason_published_at) AS worst
FROM index_humi.index_humi_agent
WHERE reason_published_at IS NOT NULL AND calculated_at > reason_published_at;

-- Stale locks: runs that died mid-batch. They self-heal on the next claim.
SELECT reason_publish_claimed_by, count(*), min(reason_publish_claimed_at)
FROM index_humi.index_humi_agent
WHERE reason_publish_claimed_at < now() - interval '2 hours'
GROUP BY 1 ORDER BY 2 DESC;
```

Schema: sibling `gsa-supabase-schema` → `supabase/docs/toast-cold-storage.md`.

## Monitoring — Agent series export (#18)

```sql
-- Cycle. status = 'closed' only when the queue emptied; it gates the future
-- wallet_daily_metrics purge by watermark.
SELECT * FROM job_control.agent_series_export_cycle ORDER BY as_of DESC LIMIT 7;

-- Queue against today's target
SELECT
  count(*)                                                                    AS agents,
  count(*) FILTER (WHERE series_export_as_of = (now() AT TIME ZONE 'utc')::date - 1) AS exported_today,
  count(*) FILTER (WHERE series_export_as_of IS NULL)                         AS never_exported,
  count(*) FILTER (WHERE series_export_claimed_at IS NOT NULL)                AS in_flight,
  min(series_export_as_of)                                                    AS oldest_cycle
FROM erc_8004.agents;

-- Objects vs agents that should have one. Expect fewer objects than exported
-- agents: those without wallets are acked without a PUT.
SELECT count(*) AS objects, pg_size_pretty(sum((metadata->>'size')::bigint)) AS total_size
FROM storage.objects WHERE bucket_id = 'agent-series';

-- Scalars freshness (step 1 runs on lane a only)
SELECT as_of, count(*) FROM erc_8004.agent_tx_scalars GROUP BY as_of ORDER BY as_of DESC;

-- Stale locks: runs that died mid-batch. They self-heal after CLAIM_STALE_SECONDS.
SELECT series_export_claimed_by, count(*), min(series_export_claimed_at)
FROM erc_8004.agents
WHERE series_export_claimed_at < now() - interval '2 hours'
GROUP BY 1 ORDER BY 2 DESC;
```

Schema: sibling `gsa-supabase-schema` → `supabase/docs/agent-series-export-storage.md`.

## Related docs

- [ARCHITECTURE.md](./ARCHITECTURE.md) — GHA pipeline and state machine
- [OPS.md](./OPS.md) — stuck wallets, URI ops, logs
- [PROCESSES.md](./PROCESSES.md) — live catalog (#10–11 URI ingest, **#13 on-demand backfill**, **#14 ERC-8257**, **#15 endpoint liveness**, **#16 Ethos reviews API**, **#17 HUMI reason publisher**, **#18 agent series export**)
- Worker READMEs under `workers/*/README.md`
- Ethos linking (schema): sibling `gsa-supabase-schema` → `supabase/docs/ethos-erc8004-linking.md`
- ERC-8183 catch-up: `supabase/docs/bsc-erc-8183-import.md` (Fase 3 = `on_demand_backfill`)
- Virtual ACP catch-up: `supabase/docs/virtual-acp-import.md` (consumer = `on_demand_backfill` / `virtual_acp_satellites`)
- Olas Mech catch-up: `supabase/docs/olas-mech-import.md` (consumer = `on_demand_backfill` / `olas_mech_satellites`)
- ERC-8257 tools: `supabase/docs/erc-8257-tools-import.md`
- Endpoint HTTP census: `supabase/docs/agent-endpoint-health.md`
- Ethos reviews API: `supabase/docs/ethos-reviews-api.md`
- Agent series export: `supabase/docs/agent-series-export-storage.md`
