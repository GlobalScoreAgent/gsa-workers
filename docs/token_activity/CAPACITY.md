# Token activity probe — capacity SLO (15d census)

Target: visit every eligible `wallet_transactions` row with the **probe** about once per **15 days**.

## Fleet (prod snapshot ~2026-07-23)

| Chain | ~WT rows | Visits / day (÷15) | GHA runners | Notes |
|-------|----------|-------------------:|------------:|-------|
| BSC | ~155k | ~10.4k | **2** | × `CONCURRENCY=2` → hasta 4 getLogs |
| Base | ~29k | ~1.9k | 1 | |
| Ethereum | ~25k | ~1.7k | 1 | |
| Arbitrum | ~16k | ~1.1k | 1 | |
| Polygon | ~14k | ~0.9k | 1 | |
| X Layer | ~8k | ~0.5k | 1 | |
| Celo | ~3k | ~0.2k | 1 | |
| Gnosis | ~1k | ~0.1k | 1 | |
| **Total** | **~252k** | **~17k** | **9** matrix | `max-parallel: 9` |

## Lookback cost

Each visit scans getLogs from `last_scanned_block+1` → tip, capped at **15 days** of blocks. If visits slip &gt;15d, raise catchup or risk a blind gap.

## GHA budget (Free ~20 concurrent)

| Control | Value |
|---------|-------|
| Runners | BSC=2, resto=1 (matrix **9**) |
| In-process `CONCURRENCY` | **2** per job |
| `strategy.max-parallel` | **9** |
| Cron | **3/9/15/21** UTC |

Claim SQL must use `awt.is_valid` (not `IS TRUE`) so `idx_agent_wallet_tx_wallet_active` applies (~80ms vs ~15s on BSC).

## Soft budget

- `MAX_RUNTIME_SECONDS=19800` (~5.5h) × 4 olas/día × jobs.
- Monitor: due count by chain; `scanned_at` age p95 ≲ 15d (esp. BSC).

## Enrich is out of this SLO

Alchemy/Etherscan enrich only processes `does_need_token_activity_enrich` rows (subset). Plan **~2** GHA slots later.
