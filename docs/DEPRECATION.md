# Deprecation notes

## pg_cron (do not re-enable)

These jobs were replaced by **inline** `wallet_apply_*_snapshot` calls in the GitHub Actions workers. Functions may remain as no-op stubs; cron should stay **disabled**.

| Cron job name | Replaced by |
|---|---|
| `wallet_update_transactions` | `wallet_nonce_balance_daily` → `wallet_apply_daily_snapshot` |
| `wallet_owner_update_transactions` | `owner_wallet_nonce_balance_monthly` → `wallet_apply_monthly_snapshot` |
| `wallet_owner_update_first_transactions` | `owner_wallet_origin` → `wallet_apply_owner_history_snapshot` |

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

## Owner wallet origin (future)

After `owner_wallet_origin` is validated in production, consider deprecating:

- Standalone `query_wallet_origin.py` CLI tool (replaced by this worker)
- Any manual origin-import scripts or one-off jobs writing `import_wallet_history_data`

## Token prices (walcert → GHA → Dex/CoinGecko)

Current: **`token_prices_import`** enriches unpriced `wallet_token_positions` via DexScreener → CoinGecko into spot cache `wallets.token_prices` (PK `chain_id`+`contract`), then `wallet_token_positions_apply_prices`.

Dune daily dumps into `wallets.token_prices` are **retired** (table redesigned 2026-07-13). Do not re-enable Dune export for this table.

| Legacy component | Status |
|---|---|
| pg_cron `walcert_token_prices_import_data` (`0 1 * * *`) | Keep **disabled**; do not re-enable |
| pg_cron `walcert_token_prices_process` (`0 13 * * *`) | Keep **disabled**; do not re-enable |
| `walcert.token_prices_import_data` (pg_net → Edge) | Superseded; leave in place until Edge retired |
| `walcert.token_prices_process` | Superseded; leave in place |
| Edge `walcert-update-token-prices` | Superseded; retire after GHA is validated |
| `walcert.token_prices` / `walcert.token_prices_imported_data` | Legacy; GHA writes go to redesigned `wallets.token_prices` |
| Dune query `7526826` → old daily `wallets.token_prices` | Retired with spot-cache redesign |

Repointing consumers that still read `walcert.token_prices` is a separate follow-up.
