# Token activity (probe → enrich)

Package under `workers/token_activity/`.

| Path | Role |
|------|------|
| [`probe/`](../../workers/token_activity/probe/) | **Live (census 15d):** public getLogs sensor; flags enrich; no transfer persist |
| `enrich/` | **Not built** — 15d flows Alchemy/Etherscan ([ENRICH.md](./ENRICH.md)) |

## Product model

1. Probe visits each wallet×chain ~every **15 days**, scanning blocks since last visit (max 15d).
2. If getLogs sees Transfer → sets `does_need_token_activity_enrich`.
3. Native nonce/balance deltas (from `wallet_daily_metrics`) enqueue enrich in the **rollup/daily metrics pipeline**, not in this probe — vault ADR 2026-07-23.
4. Once `does_need_token_activity_enrich`, probe skips that row until enrich clears it.
5. Enrich is a **subset**; do not size Alchemy Free for the full fleet.

Docs: [CAPACITY.md](./CAPACITY.md) · [PROBE_REDESIGN.md](./PROBE_REDESIGN.md) · historical [PENDING_TOKEN_ACTIVITY_RPC.md](../PENDING_TOKEN_ACTIVITY_RPC.md)

## Ops

- Workflow: `wallet-token-activity-scan.yml` → `workers/token_activity/probe`
- Matrix **7**: BSC×3 + Base×2 + ETH×1 + `_rest` (pivot eth/base/rest → BSC helper)
- Apply `20260723010000_…` (enrich flags) + `20260723060000_token_activity_matrix_7_pivot.sql`
- Capacity: [CAPACITY.md](./CAPACITY.md)
