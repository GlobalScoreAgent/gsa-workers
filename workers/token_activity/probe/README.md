# token_activity / probe (census 15d)

Public-RPC **sensor**: detect ERC-20/721 Transfer activity since `token_activity_last_scanned_block` (max **15 days** of blocks). Does **not** persist transfer rows. Enqueues enrich on Transfer (getLogs). Native nonce/balance enrich is **live** in `wallet_rollup_daily_metrics` — see vault [[Native enrich en rollup]] / ADR.

Path: `workers/token_activity/probe/`

## Pipeline

```
plan → matrix (BSC×3 + Base×2 + ETH×1 + _rest) = 7 cells
probe (CHAIN/SHARD | CHAIN=_rest) →
  claim (SKIP LOCKED; jitter; BSC advisory lock) →
  eth_getLogs Transfer (+catchup ≤15d) →
  if Transfer → flag enrich
  mark probe done: last_scanned=tip, next_eligible=+15d
  eth|base|_rest empty + time left → Pivot to BSC helper (no mod shard)
```

## Env

| Var | Default | Role |
|---|---|---|
| `SUPABASE_DB_URL` | required | Postgres |
| `CHAIN` / `SHARD` / `SHARDS` | matrix | slug, `_rest`, or dedicated shard |
| `REST_CHAINS` | celo,polygon,arbitrum,xlayer,gnosis | order for `_rest` job |
| `WALLET_BATCH_SIZE` | per-chain | getLogs sub-batch size |
| `CONCURRENCY` | **1** (GHA) | parallel sub-batches after one claim |
| `RPC_MIN_INTERVAL_MS` | **400** | pace public RPC |
| `CLAIM_JITTER_MS` | **2000** | random sleep before claim |
| `ACTIVITY_CATCHUP_MAX_DAYS` | **15** | max block lookback |
| `LOG_CHUNK_*` | per-chain | adaptive chunks |
| `MAX_RUNTIME_SECONDS` | 19800 | soft stop |
| `CLAIM_STALE_SECONDS` | 7200 | reclaim |

Secrets: only `SUPABASE_DB_URL`.

## Schema (apply first)

- `20260723010000_token_activity_probe_census_15d.sql` — enrich flags
- `20260723060000_token_activity_matrix_7_pivot.sql` — BSC=3, Base=2, ETH=1, long-tail=0

GHA: cron **3/9/15/21**, `max-parallel: 7`, `CONCURRENCY=1`. Claim: two-phase + `awt.is_valid`; BSC uses `pg_advisory_xact_lock`.

## Local

```bash
cd workers/token_activity/probe
uv sync
CHAIN=ethereum SHARD=0 SHARDS=1 uv run python job.py
# flex long-tail then BSC helper:
CHAIN=_rest SHARD=0 SHARDS=1 REST_CHAINS=celo,polygon uv run python job.py
```

## Monitoring

```sql
SELECT
  count(*) FILTER (
    WHERE token_activity_next_eligible_at IS NOT NULL
      AND token_activity_next_eligible_at <= NOW()
      AND does_need_token_activity_enrich IS NOT TRUE
  ) AS probe_due,
  count(*) FILTER (WHERE does_need_token_activity_enrich) AS enrich_pending,
  count(*) FILTER (WHERE token_activity_last_scanned_block IS NOT NULL) AS probed
FROM erc_8004.wallet_transactions;
```

## Notes

- Claim audit prefix remains `wallet_token_activity_scan/gha:…`
- Each drain starts with `clear_due_errors` for that chain (clears sticky error flags on rows already due; does not advance `next_eligible`)
- Enrich worker not built yet — flags only
- Capacity: [docs/token_activity/CAPACITY.md](../../../docs/token_activity/CAPACITY.md)
- ERC-1155 TransferSingle/Batch still deferred
