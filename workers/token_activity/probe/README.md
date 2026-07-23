# token_activity / probe (census 15d)

Public-RPC **sensor**: detect ERC-20/721 Transfer activity since `token_activity_last_scanned_block` (max **15 days** of blocks). Does **not** persist transfer rows. Enqueues 15d flow enrich when tokens moved; native nonce/balance deltas also enqueue enrich.

Path: `workers/token_activity/probe/`

## Pipeline

```
plan → matrix from chains.token_activity_runner_count
probe (CHAIN/SHARD) →
  [shard0] native gate: wallet_daily_metrics D vs D-1 → does_need_token_activity_enrich
  claim (skip enrich-pending) →
  eth_getLogs Transfer from last_scanned (+catchup ≤15d) →
  if any Transfer for wallet → flag enrich
  mark probe done: last_scanned=tip, next_eligible=+15d
```

## Env

| Var | Default | Role |
|---|---|---|
| `SUPABASE_DB_URL` | required | Postgres |
| `CHAIN` / `SHARD` / `SHARDS` | required / 0 / 1 | matrix |
| `WALLET_BATCH_SIZE` | per-chain | getLogs sub-batch size |
| `CONCURRENCY` | **4** | parallel sub-batches after one claim (`claim_limit = batch × concurrency`) |
| `ACTIVITY_CATCHUP_MAX_DAYS` | **15** | max block lookback (= census period) |
| `NATIVE_GATE_EVERY_N_LOOPS` | 1 | how often shard0 runs native enqueue |
| `LOG_CHUNK_*` | per-chain | adaptive chunks |
| `MAX_RUNTIME_SECONDS` | 19800 | soft stop |
| `CLAIM_STALE_SECONDS` | 7200 | reclaim |

Secrets: only `SUPABASE_DB_URL`.

## Schema (apply first)

Migration `20260723010000_token_activity_probe_census_15d.sql` — enrich flags.
Migration `20260723040000_token_activity_runners_one_per_chain.sql` — **1 runner/chain**; parallelism via `CONCURRENCY`.

GHA: cron **3/9/15/21**, `max-parallel: 8`, `CONCURRENCY=4`. Claim SQL uses two-phase + `awt.is_valid` (partial index).

## Local

```bash
cd workers/token_activity/probe
uv sync
CHAIN=ethereum SHARD=0 SHARDS=1 uv run python job.py
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
- Enrich worker not built yet — flags only
- Capacity: [docs/token_activity/CAPACITY.md](../../../docs/token_activity/CAPACITY.md)
- ERC-1155 TransferSingle/Batch still deferred
