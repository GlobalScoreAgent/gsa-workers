# Pending: LP positions 15-day refresh

**Status:** initial discovery **live**; **15-day refresh not built**  
**Live worker:** [`wallet_holdings_discovery`](../workers/wallet_holdings_discovery/README.md) (LP is stage 3) · catalog [#5 in PROCESSES.md](./PROCESSES.md)

## What is live

| Item | Location |
|---|---|
| Table `wallets.wallet_lp_positions` | `20260714000000` + PK/FK `20260714010000` |
| Registry `wallets.lp_pools` (`active`) | Same discovery migration (Aerodrome Base seeds) |
| Claim flags / trigger after portfolio | `does_need_lp_discovery` on `wallet_transactions` |
| GHA worker | `wallet-holdings-discovery.yml` (0/6/12/18 + dispatch); the split LP workflow was deleted 2026-09-16 |
| Docs | [PROCESSES.md](./PROCESSES.md), [SUPABASE.md](./SUPABASE.md), [ARCHITECTURE.md](./ARCHITECTURE.md), worker README |

Pipeline: claim → NFT UniV3/Pancake → classic `lp_pools` → price → upsert (stamps `calculated_at`) → mark done. Empty wallets mark done with zero rows.

## Still pending

A **separate refresh worker** that selects wallet+chain snapshots where `calculated_at <= NOW() - interval '15 days'` and recomputes LP rows (reuse `nft_lp` / `classic_lp` / `pricing`). Do **not** overload the initial `does_need_lp_discovery` one-shot flag for this.

## Acceptance checks (refresh — future)

- [ ] Stale `calculated_at` wallets are reclaimed on a schedule
- [ ] Sold NFTs / zero classic balances clear on replace upsert
- [ ] Transient DB errors retry; empty queue exits 0
