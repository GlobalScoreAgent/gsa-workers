# Wallet holdings discovery

> Project context: [AGENTS.md](../../AGENTS.md) · [Process catalog](../../docs/PROCESSES.md) · [Supabase map](../../docs/SUPABASE.md) · [Architecture](../../docs/ARCHITECTURE.md) · [Business rationale](../../docs/TOKEN_CONTRACTS_DISCOVERY_ALCHEMY.md)

**Status: live** (cron `0/6/12/18` UTC + `workflow_dispatch`). Replaces the three split workers, deleted on 2026-09-16:

- `wallet_token_contracts_discovery`
- `wallet_token_portfolio_discovery`
- `wallet_lp_positions_discovery`

For each claimed `wallet_transactions` row, runs **pending stages in succession**: contracts → portfolio → LP. A new wallet finishes all three in one run. Alchemy HTTP 429 / 5xx / timeouts are retried with `Retry-After` + exponential backoff and **do not** set `has_*_error`.

Does **not** compute WAMI / HUMI. Token USD fallback (Dex → CoinGecko) stays in `token_prices_import`. LP 15-day refresh is still planned — [PENDING_LP_POSITIONS.md](../../docs/PENDING_LP_POSITIONS.md).

## Eligibility

Claim any row with `chains.subdomain_alchemy` and **at least one** pending stage:

```sql
-- contracts pending
does_need_discovery_contracts IS DISTINCT FROM FALSE
-- or portfolio pending after contracts OK
OR (
  does_need_portfolio_discovery IS DISTINCT FROM FALSE
  AND does_need_discovery_contracts = FALSE
  AND COALESCE(has_discovery_contracts_error, FALSE) IS NOT TRUE
)
-- or LP pending after portfolio OK
OR (
  does_need_lp_discovery IS DISTINCT FROM FALSE
  AND does_need_portfolio_discovery = FALSE
  AND COALESCE(has_portfolio_discovery_error, FALSE) IS NOT TRUE
)
```

Soft lock stamps **all three** `*_claimed_at` / `*_claimed_by` so a leftover dispatch of an old worker cannot race. `claimed_by` is `wallet_holdings_discovery/gha:{WORKER_ID}`.

Triggers `trg_wallet_transactions_portfolio_flag_bu` / `_lp_flag_bu` still chain flags after a successful stage. The worker continues the remaining stages in the same process without waiting for the next cron.

## Pipeline

1. Claim `wallet_transactions` (`FOR UPDATE SKIP LOCKED`)
2. **Contracts** (if pending): Alchemy `alchemy_getTokenBalances(address, "erc20")` → `wallets.wallet_token_contracts_upsert` (insert/update; never DELETE)
3. **Portfolio** (if pending and contracts OK): load contracts → Alchemy amounts + DeFiLlama → `wallets.wallet_token_positions_insert`
4. **LP** (if pending and portfolio OK): UniV3/Pancake NFT + `wallets.lp_pools` → price → `wallets.wallet_lp_positions_upsert` (DELETE+INSERT per wallet+chain)
5. Each stage `mark_done` on success. Empty contracts / empty LP is success (`inserted=0`).

### Errors

| Kind | Examples | Persist |
|---|---|---|
| Transient | HTTP 429/503/5xx, timeouts, JSON-RPC rate-limit / CUPS | Flag stays pending; `*_claimed_at = NOW()` (stale 2h). Log `Transient wt_id=` |
| Permanent | HTTP 4xx other than rate-limit, malformed JSON-RPC, unsupported chain | `does_need_* = FALSE`, `has_*_error = TRUE`, message. Downstream stages stay blocked by existing triggers |

`src/alchemy_rpc.py` is the single JSON-RPC door (Token API + `eth_call` / Multicall3). `AlchemyTransientError` is re-raised from LP NFT/classic steps so a 429 cannot complete as “no LP”.

## Environment

| Variable | Default | Description |
|---|---|---|
| `SUPABASE_DB_URL` | required | Postgres |
| `ALCHEMY_FREE_KEY` | required | Alchemy Free (`ALCHEMY_KEY` fallback) |
| `WORKER_ID` | `discovery-a` | Suffix for `claimed_by` |
| `CONCURRENCY` | 4 | Parallel wallets (max 8) |
| `ALCHEMY_MAX_INFLIGHT` | same as `CONCURRENCY` | Cap parallel Alchemy HTTP calls |
| `CLAIM_BATCH_SIZE` | 15 | |
| `CLAIM_STALE_SECONDS` | 7200 | Re-claim after crash / after transient |
| `MAX_RUNTIME_SECONDS` | 19800 | Soft stop (~5.5h) |

Workflow: `.github/workflows/wallet-holdings-discovery.yml` (`timeout-minutes: 360`).

## Local run

```powershell
cd workers/wallet_holdings_discovery
copy .env.example .env
# Set SUPABASE_DB_URL and ALCHEMY_FREE_KEY
uv sync
uv run python job.py
```

## Monitoring / 429 reset

See [docs/SUPABASE.md](../../docs/SUPABASE.md). Rows previously burned by Alchemy 429 were requeued by schema migration `20260916183128_wallet_discovery_reset_alchemy_429.sql`, applied in prod on 2026-09-16 once this worker was live (9 210 contracts + 8 339 portfolio).

## Module layout

| File | Role |
|---|---|
| `job.py` | Claim loop; sequential stages; transient vs permanent |
| `src/db.py` | Any-pending claim; per-stage mark done/error/release |
| `src/alchemy_rpc.py` | JSON-RPC + Retry-After / backoff |
| `src/alchemy_tokens.py` | Stage 1 Token API pagination |
| `src/portfolio_calc.py` | Stage 2 fungibles |
| `src/nft_lp.py` / `classic_lp.py` / `lp_calc.py` | Stage 3 LP |
| `src/rpc.py` / `univ3_math.py` / `pricing.py` / `networks.py` | eth_call, math, Llama, chain map |
