# Probe redesign — census 15d (implemented)

Worker: `workers/token_activity/probe/`

## Behavior

| Item | Value |
|------|-------|
| Visit cadence | `next_eligible + 15 days` after successful probe |
| getLogs window | `last_scanned_block+1` → tip, floored to tip−**15d** |
| Persist transfers | **No** (sensor only) |
| Skip claim when | `does_need_token_activity_enrich IS TRUE` |
| Token signal | any ERC-20/721 Transfer → set enrich flag |
| Native signal | `wallet_daily_metrics` D vs D−1 (shard 0) → enrich flag |
| Catch-up env | `ACTIVITY_CATCHUP_MAX_DAYS=15` |

## Enqueue

```text
does_need_token_activity_enrich =
  probe_had_Transfer
  OR native_nonce_or_balance_delta
```

(`never_enriched` backfill for enrich worker is ops/future.)

## Schema

`gsa-supabase-schema` migration `20260723010000_token_activity_probe_census_15d.sql`

## Still deferred

- ERC-1155 TransferSingle/Batch in probe
- Enrich worker (`workers/token_activity/enrich/`)
- Staggered GHA cron vs other jobs

See [CAPACITY.md](./CAPACITY.md) · [ENRICH.md](./ENRICH.md)
