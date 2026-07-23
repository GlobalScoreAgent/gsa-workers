# Token activity (probe → enrich)

Package under `workers/token_activity/`.

| Path | Role |
|------|------|
| [`probe/`](../../workers/token_activity/probe/) | **Live (census 15d):** public getLogs sensor; flags enrich; no transfer persist |
| `enrich/` | **Not built** — 15d flows Alchemy/Etherscan ([ENRICH.md](./ENRICH.md)) |

## Product model

1. Probe visits each wallet×chain ~every **15 days**, scanning blocks since last visit (max 15d).
2. Daily **native** deltas also enqueue enrich without waiting for getLogs.
3. Once `does_need_token_activity_enrich`, probe skips that row until enrich clears it.
4. Enrich is a **subset**; do not size Alchemy Free for the full fleet.

Docs: [CAPACITY.md](./CAPACITY.md) · [PROBE_REDESIGN.md](./PROBE_REDESIGN.md) · historical [PENDING_TOKEN_ACTIVITY_RPC.md](../PENDING_TOKEN_ACTIVITY_RPC.md)

## Ops

- Workflow: `wallet-token-activity-scan.yml` → `workers/token_activity/probe`
- Apply schema migration `20260723010000_token_activity_probe_census_15d.sql` before relying on enrich flags
- Often **`disabled_manually`** in GHA until capacity/cron window is ready
