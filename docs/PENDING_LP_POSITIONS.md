# Pending: LP positions 15-day refresh

**Status:** initial discovery **live** (`wallet_lp_positions_discovery`); **15-day refresh not built**  
**Depends on (live):** `wallet_lp_positions_discovery`, `wallets.wallet_lp_positions.calculated_at`, `wallets.lp_pools`

## What is live

| Item | Location |
|---|---|
| Table `wallets.wallet_lp_positions` | Schema migration `20260714000000_wallet_lp_positions_discovery` |
| Registry `wallets.lp_pools` (`active`) | Same migration (Aerodrome Base seeds) |
| Claim flags / trigger after portfolio | `does_need_lp_discovery` on `wallet_transactions` |
| Worker | [`workers/wallet_lp_positions_discovery`](../workers/wallet_lp_positions_discovery/README.md) |
| Docs | [PROCESSES.md](./PROCESSES.md) #8, [SUPABASE.md](./SUPABASE.md) |

Pipeline: claim → NFT UniV3/Pancake → classic pools from DB → price → upsert (stamps `calculated_at`) → mark done.

## Still pending

A **separate refresh worker** that selects wallet+chain snapshots where `calculated_at <= NOW() - interval '15 days'` and recomputes LP rows (reuse `nft_lp` / `classic_lp` / `pricing` modules). Do **not** overload the initial `does_need_lp_discovery` one-shot flag for this.

## Acceptance checks (refresh — future)

- [ ] Stale `calculated_at` wallets are reclaimed on a schedule
- [ ] Sold NFTs / zero classic balances clear on replace upsert
- [ ] Transient DB errors retry; empty queue exits 0
