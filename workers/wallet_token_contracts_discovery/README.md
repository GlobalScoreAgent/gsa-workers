# Wallet token contracts discovery

> Project context: [AGENTS.md](../../AGENTS.md) · [Supabase map](../../docs/SUPABASE.md) · [Architecture](../../docs/ARCHITECTURE.md)

Claim worker that discovers ERC-20 contracts with **balance > 0** for each `erc_8004.wallet_transactions` row whose chain has `subdomain_alchemy` set. Stores addresses only (no balances) in `wallets.wallet_token_contracts`.

## Eligibility

```sql
wt.does_need_discovery_contracts IS DISTINCT FROM FALSE
AND c.subdomain_alchemy IS NOT NULL AND btrim(c.subdomain_alchemy) <> ''
AND (
  wt.discovery_contracts_claimed_at IS NULL
  OR wt.discovery_contracts_claimed_at < NOW() - interval '2 hours'
)
```

| Flag | Meaning |
|---|---|
| `NULL` / `TRUE` | Pending discovery |
| `FALSE` | Done for this wallet+chain |

Chains without Alchemy subdomain (e.g. X Layer today) are marked `FALSE` and never claimed. Enabling a chain later: set `subdomain_alchemy` then `UPDATE … SET does_need_discovery_contracts = TRUE WHERE chain_id = ?`.

## Pipeline

1. Claim `wallet_transactions` rows (`FOR UPDATE SKIP LOCKED`) joining `chains` + `wallets`
2. Alchemy `alchemy_getTokenBalances(address, "erc20")` on `https://{subdomain}.g.alchemy.com/v2/{ALCHEMY_FREE_KEY}` (paginate `pageKey`)
3. Keep contracts with hex balance > 0
4. `wallets.wallet_token_contracts_replace(wallet_id, chain_id, rows)` (delete+insert for that pair)
5. Set `does_need_discovery_contracts = FALSE`, clear claim columns

On Alchemy/DB error: clear claim (leave flag pending), continue loop (exit 0). Exit 0 if claim empty or time budget.

## Manual re-queue

```sql
UPDATE erc_8004.wallet_transactions
SET does_need_discovery_contracts = TRUE,
    discovery_contracts_claimed_at = NULL,
    discovery_contracts_claimed_by = NULL
WHERE chain_id = <internal_chain_id>;
```

## Monitoring

```sql
SELECT
  count(*) FILTER (WHERE does_need_discovery_contracts IS DISTINCT FROM FALSE) AS pending,
  count(*) FILTER (WHERE does_need_discovery_contracts = FALSE) AS done
FROM erc_8004.wallet_transactions;

SELECT count(*) FROM wallets.wallet_token_contracts;
```

## Environment

| Variable | Default | Description |
|---|---|---|
| `SUPABASE_DB_URL` | required | Postgres connection string |
| `ALCHEMY_FREE_KEY` | required | Alchemy API key (`ALCHEMY_KEY` accepted as fallback) |
| `WORKER_ID` | `discovery-a` | Claimed-by label |
| `CONCURRENCY` | 10 | Parallel rows (max 20) |
| `CLAIM_BATCH_SIZE` | 50 | Rows per claim batch |
| `CLAIM_STALE_SECONDS` | 7200 | Re-claim delay after crash |
| `MAX_RUNTIME_SECONDS` | 19800 | Soft stop (~5.5h) |
