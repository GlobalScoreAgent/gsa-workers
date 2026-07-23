# Token activity probe — capacity SLO (15d census)

Target: visit every eligible `wallet_transactions` row with the **probe** about once per **15 days**.

## Fleet (prod snapshot ~2026-07-23)

| Chain | ~WT rows | Visits / day (÷15) | GHA role | Notes |
|-------|----------|-------------------:|----------|-------|
| BSC | ~155k | ~10.4k | **3 dedicated** + helpers | Eth/Base/`_rest` pivot here when empty |
| Base | ~29k | ~1.9k | **2 dedicated** | Pivot → BSC helper |
| Ethereum | ~25k | ~1.7k | **1 dedicated** | Pivot → BSC helper |
| Celo / Polygon / Arb / XL / Gnosis | ~42k | ~2.8k | **`_rest` ×1** (serie) | Luego pivot → BSC helper |
| **Total** | **~252k** | **~17k** | **7** matrix | `max-parallel: 7` |

## Matrix (exact 7)

`build_matrix.py` emits shards where `token_activity_runner_count >= 1` plus fixed `{chain:"_rest"}`. Fails if `len != 7`.

| Celdas | Rol |
|--------|-----|
| BSC × 3 | Shards 0..2 (native gate solo shard 0) |
| Base × 2 | Shards 0..1 |
| ETH × 1 | Shard 0 |
| `_rest` × 1 | `REST_CHAINS`: celo → polygon → arbitrum → xlayer → gnosis |

**Pivot:** cuando eth / base / `_rest` vacían due y queda `MAX_RUNTIME`, claman BSC **sin** `mod(wallet_id, shards)` (`helper=true`, `SKIP LOCKED`), coexistiendo con s0/s1/s2.

## Claim guard (7 jobs)

| Mitigación | Detalle |
|------------|---------|
| Claim barato | CTE `MATERIALIZED` + `awt.is_valid` (~80ms) |
| `SKIP LOCKED` | Dedicados y helpers no se bloquean por fila |
| Jitter | `CLAIM_JITTER_MS=2000` antes de cada claim |
| Advisory | `pg_advisory_xact_lock` en claims BSC (dedicados + helpers) |

## GHA budget (Free ~20 concurrent)

| Control | Value |
|---------|-------|
| Runners DB | BSC=3, Base=2, ETH=1, long-tail=0 |
| In-process `CONCURRENCY` | **1** per job |
| `strategy.max-parallel` | **7** |
| Cron | **3/9/15/21** UTC |
| Reserva enrich | ~2 slots (worker aún no built) |

## Soft budget

- `MAX_RUNTIME_SECONDS=19800` (~5.5h) × 4 olas/día × 7 jobs.
- Monitor: due por chain; age p95 ≲ 15d (esp. BSC); logs `Pivot to BSC helper`.

## Enrich is out of this SLO

Alchemy/Etherscan enrich only processes `does_need_token_activity_enrich` rows. Plan **~2** GHA slots later.
