# Enrich 15d (design — not built)

Pull wallet×chain **flows for the last ~15 days** only for rows enqueued by the gate (probe + daily metrics).

## Providers (hypothesis)

| Chains | Provider |
|--------|----------|
| ETH, Arb, Polygon, Celo, Gnosis | Etherscan V2 (Free/Lite+) |
| BSC, Base | Alchemy `alchemy_getAssetTransfers` |
| X Layer | OKLink / TBD |

## Alchemy call (activity, not funding)

- Categories: `["external", "erc20", "erc721", "erc1155"]`
- Lookback ≈ 15d (not `fromBlock: 0x0` — that is **funding**)
- Typically **two** calls: `fromAddress` + `toAddress` (~120 CU each on Free)
- Paginate with `pageKey` as needed

## Etherscan

- Actions covering native + ERC-20/721/1155 (several calls per wallet×chain)
- Cap ~100k calls/day (~3 rps) — binds wall-clock more than Alchemy for non-BSC chains

## Capacity (order of magnitude, vault + prod deltas)

- Full fleet enrich without gate: Alchemy BSC+Base dominates Free CU (~weeks).
- Native day-over-day signal on measured pairs (~2026-07-21→22): ~**4.6%** overall, BSC ~**7%** — gate helps a lot for **recurring** refresh; **never_enriched** backfill still heavy.

## Worker sketch

- Folder: `workers/token_activity/enrich/` (TBD)
- Claim: `does_need_token_activity_enrich IS TRUE` (+ enrich claim cols from migration `20260723010000_…`)
- On success: clear flag, set `token_activity_enrich_completed_at`, schedule enrich cooldown
- Separate GHA workflow, low concurrency, respect CU / Etherscan rps
- Do **not** mix getLogs probe and Transfers in the same hot path

Related: probe flags already written by `workers/token_activity/probe`.
