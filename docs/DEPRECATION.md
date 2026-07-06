# Deprecation notes (Phase 2)

After `wallet_nonce_balance_daily` is validated in production:

## Cloudflare

- Worker: `wallet-snapshot` in `gsa-cloudflare-workers`
- Route: `api.globalscoreagent.com/wallet-snapshot*`
- Action: disable route and retire worker when GitHub Actions job covers all use cases

## Supabase Edge Functions

- `wallets-query-snapshot` — proxy to Cloudflare Worker
- `wallet-transactional-current-batch` — remains in use for `wallet_transactional_details` (different table/flow)

Only remove components after confirming no external consumers depend on them.

## Owner wallet origin (future)

After `owner_wallet_origin` is validated in production, consider deprecating:

- Standalone `query_wallet_origin.py` CLI tool (replaced by this worker)
- Any manual origin-import scripts or one-off jobs writing `import_wallet_history_data`
