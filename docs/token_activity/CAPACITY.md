# Token activity probe — capacity SLO (15d census)

Target: visit every eligible `wallet_transactions` row with the **probe** about once per **15 days**.

## Fleet (prod snapshot ~2026-07-23)

| Chain | ~WT rows | % | Ideal of 7 | Runners |
|-------|----------|--:|----------:|--------:|
| BSC | 155k | 61.6% | 4.31 | **4** |
| Base | 29k | 11.4% | 0.80 | **1** |
| Ethereum | 25k | 10.1% | 0.71 | **1** |
| Arbitrum | 16k | 6.5% | 0.46 | **1** |
| Polygon | 14k | 5.4% | 0.38 | **1** |
| X Layer | 8k | 3.2% | 0.23 | **1** |
| Celo | 3k | 1.1% | 0.08 | **1** |
| Gnosis | 1k | 0.5% | 0.04 | **1** |
| **Total** | **~252k** | 100% | 7 | **11** matrix / **≤7** concurrent |

Visits / day target (÷15): **~17k**.

## Lookback cost

Each visit scans getLogs from `last_scanned_block+1` → tip, capped at **15 days** of blocks (not 1d). Worst case ≈ one full 15d window per wallet per period (same total blocks as daily 1d visits).

## GHA budget (Free ~20 concurrent)

Assumptions: base fleet peak ~11 jobs at 0/6/12/18; reserve **2** for future enrich → **~7** concurrent for probe.

| Control | Value |
|---------|-------|
| Formula | `runners = max(1, round(7 × wt_share))` |
| Matrix cells | **11** (covers all active chains) |
| `strategy.max-parallel` | **7** (hard concurrent cap) |
| Cron | **3/9/15/21** UTC (stagger vs 0/6/12/18) |

Why not sum runners = 7 exactly? Eight chains × min 1 already needs 8; pure `% × 7` would zero Arb/Poly/XL/Celo/Gnosis. Min-1 + `max-parallel: 7` covers all without exceeding the Free slot budget.

Soft math: 7 jobs × ~5.5h × 4 waves ≈ **~150 job-horas/día** → need ~**1.7–2 WT/min/job** to hit ~17k/day.

## Soft budget

- `MAX_RUNTIME_SECONDS=19800` (~5.5h) × 4 slots/day × ≤7 concurrent ≈ shard-hours available.
- Monitor: due count by chain must not trend up unbounded; `scanned_at` age p95 ≲ 15d (esp. BSC).
- Recompute runners when fleet mix shifts a lot (`supabase/scripts/token_activity_runners_by_pct.sql`).

## Enrich is out of this SLO

Alchemy/Etherscan enrich only processes `does_need_token_activity_enrich` rows (subset). Plan **~2** GHA slots later; do **not** size Free CU for full-fleet enrich.
