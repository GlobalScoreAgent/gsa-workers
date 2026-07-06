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
