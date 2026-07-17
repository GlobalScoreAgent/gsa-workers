# wallet_token_activity_scan

Incremental **token Transfer** activity (ERC-20 / ERC-721) via **public** `eth_getLogs` — no Alchemy.

Complements `wallet_token_contracts_discovery` (Alchemy current balances).

## Pipeline

```
plan job → matrix from chains.token_activity_runner_count
scan job (CHAIN + SHARD + SHARDS) →
  claim 50 wallets (valid agents + awt.is_valid + shard mod) →
  eth_getLogs Transfer from/to (batch OR topics) →
  classify 3 topics=erc20 / 4+=erc721 →
  upsert contracts + nft_contracts + transfers →
  advance last_scanned_block; next_eligible_at += 1 day
```

## Env

| Var | Default | Role |
|---|---|---|
| `SUPABASE_DB_URL` | required | Postgres |
| `CHAIN` | required | slug: ethereum, base, arbitrum, polygon, bsc, celo, gnosis, xlayer |
| `SHARD` / `SHARDS` | `0` / `1` | partition `mod(wallet_id, SHARDS)=SHARD` |
| `WALLET_BATCH_SIZE` | 50 | wallets per claim / getLogs OR filter |
| `ACTIVITY_CATCHUP_MAX_DAYS` | 3 | max block lookback when cursor NULL/behind |
| `LOG_CHUNK_*` | 2000 / 50 / 10000 | adaptive block chunking |
| `RPC_MIN_INTERVAL_MS` | 150 | pacing between public RPC calls |
| `MAX_RUNTIME_SECONDS` | 19800 | soft stop |
| `CLAIM_STALE_SECONDS` | 7200 | reclaim in-flight |

Secrets: **only** `SUPABASE_DB_URL` (no `ALCHEMY_*`).

## Local

```bash
cd workers/wallet_token_activity_scan
uv sync
# after schema deploy + optional seed of next_eligible_at
CHAIN=ethereum SHARD=0 SHARDS=1 uv run python job.py
```

Build matrix:

```bash
uv run python scripts/build_matrix.py
```

## Queue seed

Migration does **not** enqueue existing `wallet_transactions`. New inserts get `-infinity` via BI trigger.

To enqueue existing rows (mass re-queue — ask first):

`gsa-supabase-schema/supabase/scripts/wallet_token_activity_scan_seed_queue.sql`

## Monitoring

```sql
SELECT
  count(*) FILTER (WHERE token_activity_next_eligible_at IS NOT NULL
                   AND token_activity_next_eligible_at <= NOW()) AS due,
  count(*) FILTER (WHERE token_activity_next_eligible_at IS NULL) AS not_queued,
  count(*) FILTER (WHERE has_token_activity_error IS TRUE) AS errors,
  count(*) FILTER (WHERE token_activity_last_scanned_block IS NOT NULL) AS scanned
FROM erc_8004.wallet_transactions;

SELECT c.name, c.token_activity_runner_count
FROM erc_8004.chains c
WHERE c.is_active
ORDER BY c.id;
```

## Out of scope (v1)

ERC-1155 events, native/external transfers, Alchemy Transfers / getTokenBalances.

## Notes

On provider errors like Cloudflare `-32047` / `range too large`, the scanner shrinks the block chunk (down to 800 when advertised) instead of rotating RPCs forever.
