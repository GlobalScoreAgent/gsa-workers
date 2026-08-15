# ERC-8257 tools import (`erc8257_tools_import`)

Sync the public [agenttoolindex.xyz](https://agenttoolindex.xyz) catalog into `erc_8257.tools`.

**Status:** Live (2026-08-15). Validated GHA run: https://github.com/GlobalScoreAgent/gsa-workers/actions/runs/31860915582  
**ADR:** vault `08 - Decisiones/2026-08-13 - Indexar ERC-8257 Base y Ethereum`  
**Schema:** `gsa-supabase-schema` → `supabase/docs/erc-8257-tools-import.md`  
**Vault ops:** `12 - Github Worker/ERC-8257 Tools/`

## Pipeline

```
GET /api/stats → compare erc_8257.sync_state.source_synced_at
  → skipped_unchanged (exit 0) OR
  → GET /api/tools?status=active&limit=500
  → GET /api/tools?status=deregistered&limit=500
  → merge by (chain_id, id)
  → erc_8257.tools_upsert
  → update sync_state
```

- Full catalog ingest (all chains returned by the API). Filter Base (`8453`) / Ethereum (`1`) only in read metrics.
- `creator` may be NULL on deregistered tools (source behavior); still upserted.
- No API key. Watermark: `stats.synced_at` (API has no real delta / `offset` is ignored).
- Force: `FORCE_FULL_SYNC=1` or workflow input `force=true`.

## Schedule

| Trigger | When |
|---------|------|
| Cron | `0 4 * * *` UTC (daily off-peak) |
| Manual | `workflow_dispatch` (+ optional `force`) |

Workflow: `.github/workflows/erc8257-tools-import.yml`  
Concurrency: `erc8257-tools-import`

## Env

| Variable | Required | Default |
|----------|----------|---------|
| `SUPABASE_DB_URL` | yes | — |
| `AGENTTOOLINDEX_BASE_URL` | no | `https://agenttoolindex.xyz` |
| `UPSERT_CHUNK_SIZE` | no | `5000` |
| `FORCE_FULL_SYNC` | no | `0` |

## Local

```bash
cd workers/erc8257_tools_import
cp .env.example .env   # set SUPABASE_DB_URL
uv sync
uv run python job.py
```

## Coverage snapshot (prod, 2026-08-15)

| Metric | Value |
|--------|------:|
| Tools | 622 |
| Active Base+Eth | 408 |
| Linked wallets | 207 |
| Distinct GSA creators | 58 |
| Agents with owner publisher | 304 |

## Monitoring SQL

```sql
SELECT * FROM erc_8257.sync_state;

SELECT chain_id, chain_name, status, count(*)
FROM erc_8257.tools
GROUP BY 1, 2, 3
ORDER BY 1, 3;

-- HUMI-oriented coverage (Base + Eth only)
SELECT
  count(*) AS tools,
  count(*) FILTER (WHERE creator_wallet_id IS NOT NULL) AS linked,
  count(DISTINCT creator) FILTER (WHERE creator_wallet_id IS NOT NULL) AS creators_in_gsa
FROM erc_8257.tools
WHERE chain_id IN (1, 8453)
  AND status = 'active';
```
