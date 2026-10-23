# Deprecation notes

## WAMI workers (removed 2026-10-21 — ADR *Deprecar WAMI y mover analisis a Walcert*)

The four GitHub Actions workers that fed the WAMI wallet index were **deleted**. WAMI was deprecated as an always-on precomputed score (scores stale since 2026-07-06); deep wallet analysis (origins, activity, multichain, portfolio) is now exclusively **Walcert Agent on-demand** (preview + x402 certificate). HUMI remains the always-on agent score.

Their database destinations were dropped in `gsa-supabase-schema` on 2026-10-22 (migrations `20261022020359`…`20261022022035`, docs `supabase/docs/wami-deprecation-drop.md`), so these workers were failing against nonexistent RPCs/tables. Do **not** revive them, their workflows, or the dropped objects:

| Worker (deleted) | Workflow (deleted) | Dropped destination |
|---|---|---|
| `workers/wallet_holdings_discovery/` | `wallet-holdings-discovery.yml` | `wallets.wallet_token_contracts`, `wallet_token_positions` (incl. `token_quality`/`quality_reason` — Portfolio Quality source data), `wallet_lp_positions`, `wallet_token_transfers`, `wallet_nft_contracts`, `lp_pools` + discovery claim columns on `wallet_transactions` |
| `workers/token_prices_import/` | `token-prices-import.yml` | `wallets.token_prices` + `token_prices_upsert` / `wallet_token_positions_apply_prices` / `wallet_token_positions_mark_price_misses` |
| `workers/wallet_activity_flows/` (+ smoke) | `wallet-activity-flows.yml`, `wallet-activity-flows-smoke.yml` | Staging `wallets.wallet_activity_transfers` + `activity_flows_*` claim columns |
| `workers/wallet_funding_transfers/` | `wallet-funding-transfers.yml` | `wallets.wallet_funding_transfers` + `wallet_origins_stats` prep (Origins pillar) + `funding_transfers_*` claim columns |

Notes:

- The Portfolio Quality prep table `wallets.wallet_quality_stats` never existed (deferred stub in the dropped `job_control.wami_stats_pipeline_schedule`); Quality's source data above is what was dropped.
- `dune_queries_import` was **kept** through the WAMI drop (tables fed `walcert_lookup_labels`). **Retired 2026-10-22** when Walcert cut over to Walpulse `quality-lookup` (iq-v1): tables + RPC dropped (`20261022233000_walcert_drop_gsa_label_stack`); workflow schedule removed. `wallet_nonce_balance_daily` / `owner_wallet_monthly` still stay (HUMI core).
- GHA secrets that only these workers used (`ALCHEMY_FREE_KEY`, `COINGECKO_KEY`, activity/funding provider keys, OKX trio) are inert; removing them from repo Settings is optional manual cleanup.
- Old code and design docs (`docs/TOKEN_CONTRACTS_DISCOVERY_ALCHEMY.md`, `docs/PENDING_LP_POSITIONS.md`, both deleted) remain readable in git history.

## Dune queries import / GSA label tables (retired 2026-10-22)

Walcert Origins/Activity labels now come exclusively from **Walpulse** `POST …/quality-lookup` (`iq-v1`, `include_kleros: true`). Do **not** restore these objects:

| Object | Status |
|--------|--------|
| Workflow `dune-queries-import.yml` | Schedule removed; dispatch refuses |
| Worker `workers/dune_queries_import/` | Code retained for history; do not schedule |
| `wallets.cex_addresses` / `mixer_addresses` / `bridge_addresses` / `ofac_sanction_addresses` / `kleros_scout_addresses` | DROPped |
| `walcert.walcert_lookup_labels` + upsert RPCs | DROPped |
| `graphs.normalize_batch_kleros_scout_litem` | No-op (marks import completed) |

Migration/docs: `gsa-supabase-schema` → `supabase/migrations/20261022233000_walcert_drop_gsa_label_stack.sql`, `supabase/docs/walcert-drop-gsa-label-stack.md`.

## Ethos enrich workflow (absorbed by on-demand backfill)

| Legacy | Status |
|---|---|
| Workflow `ethos-enrich.yml` | **Removed** — replaced by `on-demand-backfill.yml` |
| Worker folder `workers/ethos_enrich/` | Thin shim / reference only; live code in `workers/on_demand_backfill/` |

Do **not** re-add a dedicated Ethos enrich cron. Steps `ethos_history` + `ethos_scores` run inside the orchestrator.

The dedicated worker `ethos_reviews_api` (`ethos-reviews-api.yml`) is **not** that enrich job: it only fills `ethos.reviews` via Ethos API v2 (ADR 2026-08-25) after Goldsky dropped review entities.

## pg_cron (do not re-enable)

These jobs were replaced by **inline** `wallet_apply_*_snapshot` calls in the GitHub Actions workers. Functions may remain as no-op stubs; cron should stay **disabled**.

| Cron job name | Replaced by |
|---|---|
| `wallet_update_transactions` | `wallet_nonce_balance_daily` → `wallet_apply_daily_snapshot` |
| `wallet_owner_update_transactions` | `owner_wallet_monthly` lane `monthly` → `wallet_apply_monthly_snapshot` |
| `wallet_owner_update_first_transactions` | `owner_wallet_monthly` lane `origin` → `wallet_apply_owner_history_snapshot` |

`wallet_hourly_process` no longer toggles those OwnerTx / daily snapshot crons.

## Phase 2 — Cloudflare / Edge (after daily is stable)

After `wallet_nonce_balance_daily` is validated in production:

### Cloudflare

- Worker: `wallet-snapshot` in `gsa-cloudflare-workers`
- Route: `api.globalscoreagent.com/wallet-snapshot*`
- Action: disable route and retire worker when GitHub Actions covers all use cases

### Supabase Edge Functions

- `wallets-query-snapshot` — proxy to Cloudflare Worker (deprecate with daily)
- `wallet-transactional-current-batch` — **remains** for `wallet_transactional_details` (different table/flow)

Only remove components after confirming no external consumers depend on them.

## Split owner wallet workers (replaced by owner_wallet_monthly, 2026-09-16)

`owner_wallet_nonce_balance_monthly` and `owner_wallet_origin` were merged into the single worker **`owner_wallet_monthly`**, which runs both tasks as concurrent lanes (ADR *Unificar workers owner wallet monthly y origin*). Same gate, same 30-day cadence, same `ALCHEMY_KEY`; the data model is unchanged (two clocks, two payloads, two snapshots).

| Legacy | Status |
|---|---|
| Workflow `owner-wallet-nonce-balance-monthly.yml` | **Deleted** — replaced by `owner-wallet-monthly.yml` |
| Workflow `owner-wallet-origin.yml` | **Deleted** — replaced by `owner-wallet-monthly.yml` |
| Worker folder `workers/owner_wallet_nonce_balance_monthly/` | **Deleted** |
| Worker folder `workers/owner_wallet_origin/` | **Deleted** |
| Standalone `query_wallet_origin.py` CLI tool | Superseded by the `origin` lane |
| Manual origin-import scripts writing `import_wallet_history_data` | Superseded by the `origin` lane |

Do **not** restore the two workflows or folders while `owner-wallet-monthly` is live: three jobs claiming the same wallets on the same two clocks would double-spend the Alchemy budget and reintroduce the uncoordinated 429 bursts the merge was meant to fix.

## Agent URI ingest (Edge → GHA)

Current: **`agent_uri_resolve`** (00:00 / 12:00 UTC) materializes agents + feedbacks into `uri_documents` / `agent_manifest`. **`agent_uri_reprocess`** (06:00 / 18:00) retries download errors and refreshes off-chain HTTP/IPFS docs older than 15 days.

Do **not** re-enable Edge URI batch processors or legacy manifest reprocess cron for ingest. Canonical JSON lives in `uri_documents` (`uri_hash`); `agent_manifest.data` / `url` are dropped.

| Legacy component | Status |
|---|---|
| Edge `agent-uri-batch-processor` | Superseded by `agent_uri_resolve`; keep **disabled** |
| Edge `feedback-uri-batch-processor` | Superseded by `agent_uri_resolve`; keep **disabled** |
| Edge `agent-process-uri` (ingest path) | Superseded; keep **disabled** for ingest |
| pg_cron / SPs that reprocessed `agent_manifest.url` / `data` | Superseded by `agent_uri_reprocess`; leave off |
| Manifest **entity consume** pg_cron (profile / feedbacks / liveness / sentinel) | Still **off** until SQL readers JOIN `uri_documents`; not replaced by these two workers |

Ops may delete leftover URI Edge/cron artifacts in the schema repo; workers do not call those stubs.

### Scrape.do → ScrapingAnt (2026-08-15)

URI HTTP last-fallback moved from `SCRAPE_DO_TOKEN` / `scrape/scrape_do.py` to `SCRAPING_ANT_KEY` / `scrape/scraping_ant.py`. Do not reintroduce Scrape.do in workflows. Historical `source_gateway` values `scrape-do-*` remain in `uri_documents`; new successes use `scraping-ant-*`.

### Pinata dedicated gateway host (2026-08-15)

IPFS cascade: public gateways first (`ipfs.io` → `gateway.pinata.cloud` → Cloudflare → dweb), then **Pinata dedicated** last if `PINATA_GATEWAY` is set. Host is `violet-efficient-aardwolf-922.mypinata.cloud` (replaces retired `indigo-urban-flyingfish-439`). Secret value is the **Gateway Key** (`x-pinata-gateway-token`), not Pinata API Key/Secret/JWT. Historical `pinata-dedicated` rows keep the old host in past fetches only via `source_gateway` label (same name).

## Token prices (walcert → GHA → Dex/CoinGecko)

**Superseded by the WAMI removal above (2026-10-21):** `token_prices_import`, `wallets.token_prices` and `wallets.wallet_token_positions` were all deleted/dropped; this section is historical. The historical path enriched unpriced `wallet_token_positions` via DexScreener → CoinGecko into spot cache `wallets.token_prices` (PK `chain_id`+`contract`), then `wallet_token_positions_apply_prices`.

Dune daily dumps into `wallets.token_prices` are **retired** (table redesigned 2026-07-13). Do not re-enable Dune export for this table.

| Legacy component | Status |
|---|---|
| pg_cron `walcert_token_prices_import_data` (`0 1 * * *`) | Keep **disabled**; do not re-enable |
| pg_cron `walcert_token_prices_process` (`0 13 * * *`) | Keep **disabled**; do not re-enable |
| `walcert.token_prices_import_data` (pg_net → Edge) | Superseded; leave in place until Edge retired |
| `walcert.token_prices_process` | Superseded; leave in place |
| Edge `walcert-update-token-prices` | Superseded; retire after GHA is validated |
| `walcert.token_prices` / `walcert.token_prices_imported_data` | Legacy; dropped with the legacy WAMI `walcert` schema cleanup (2026-10-22) |
| Dune query `7526826` → old daily `wallets.token_prices` | Retired with spot-cache redesign |


## Token activity probe / enrich (replaced by wallet activity flows 15d)

Census getLogs probe + enrich-flag pipeline was removed (ADR 2026-08-13). Do **not** revive it.

| Legacy | Status |
|---|---|
| Workflow `wallet-token-activity-scan.yml` | **Removed** — was replaced by `wallet-activity-flows.yml`, itself deleted with WAMI (see above) |
| Worker folder `workers/token_activity/` (probe; enrich never existed) | **Removed** |
| Docs `docs/token_activity/` + `docs/PENDING_TOKEN_ACTIVITY_RPC.md` | **Removed** |
| Columns `token_activity_*` / `does_need_token_activity_enrich` / `chains.token_activity_runner_count` | Dropped in schema migration `20260813010000_wallet_activity_transfers.sql` |
| Rollup Fuente B (`does_need_token_activity_enrich` on D vs D−1) | Removed from `wallet_rollup_daily_metrics` |

Former replacement `workers/wallet_activity_flows/` was deleted with WAMI (see above); its staging `wallets.wallet_activity_transfers` was dropped 2026-10-22. Do not re-add probe getLogs, enrich flags, or Fuente B enqueue.

## Walcert Alchemy fund origins (replaced by GHA funding transfers)

**Superseded by the WAMI removal above (2026-10-21):** the GHA replacement `wallet_funding_transfers` was also deleted and `wallets.wallet_funding_transfers` dropped; wallet origin analysis is Walcert on-demand. Historically, first-inflow ingest was **`wallet_funding_transfers`** (Etherscan / Blockscout / Ankr / OKX → `wallets.wallet_funding_transfers`). Do **not** re-enable the Walcert Alchemy Edge + pg_cron path.

| Legacy | Status |
|---|---|
| Edge `walcert-alchemy-fund-origins` | Keep **disabled**; GHA worker is the ingest path |
| pg_cron `walcert_alchemy_fund_origins_import_data` / `analyze_fund_origins` / first-activity | Stay **`active = false`** |
| `walcert.wallet_imported_data.fund_origins_json` | Legacy JSON dump; dropped with the legacy WAMI `walcert` cleanup (2026-10-22) |
| `walcert.wallet_fund_origins` | Historical analyze output; dropped 2026-10-22 |

## Split token/LP discovery workers (replaced by holdings discovery)

**Superseded by the WAMI removal above (2026-10-21):** the unified replacement `wallet_holdings_discovery` was also deleted. ADR 2026-09-16 had merged the three split workers into **`wallet_holdings_discovery`** (contracts → portfolio → LP in one run, Alchemy 429 backoff). Do **not** re-enable any of them.

| Legacy | Status |
|---|---|
| Workflow `wallet-token-contracts-discovery.yml` | **Deleted** 2026-09-16 |
| Workflow `wallet-token-portfolio-discovery.yml` | **Deleted** 2026-09-16 |
| Workflow `wallet-lp-positions-discovery.yml` | **Deleted** 2026-09-16 |
| Folders `workers/wallet_token_contracts_discovery/`, `wallet_token_portfolio_discovery/`, `wallet_lp_positions_discovery/` | **Deleted** 2026-09-16; unified successor `workers/wallet_holdings_discovery/` deleted 2026-10-21 (WAMI) |

Do not re-add them. If you need the old code, read it from git history (`git log -- workers/wallet_lp_positions_discovery` or `git log -- workers/wallet_holdings_discovery`).

429-burned rows were requeued only after the unified worker ran clean once: schema `20260916183128_wallet_discovery_reset_alchemy_429.sql` (prod 2026-09-16). Historical only — the discovery claim columns were dropped 2026-10-22.

