# On-demand backfill

Orquestador GHA multi-step: catch-up on-demand para dominios con flag `needs_*`.

## Steps (secuenciales)

| Step | Acción | Empty → |
|------|--------|---------|
| `ethos_history` | `claim_history_fetch` → Goldsky Ethos → upsert señales → complete | skip |
| `ethos_scores` | `list_score_candidates` → Ethos API → `upsert_official_scores` | skip |
| `erc8183_satellites` | `claim_satellite_backfill` → Goldsky ERC-8183 → upsert satélites → complete (incluso 0 eventos) | skip |
| `virtuals_acp` | stub | skip |
| `olas_marketplace` | stub | skip |

Error en un step: log + **continúa** al siguiente. Presupuesto global `MAX_RUNTIME_SECONDS`.

## Env

Ver `.env.example`. Requerido: `SUPABASE_DB_URL`.

## Local

```bash
cd workers/on_demand_backfill
uv sync
uv run python job.py
```

## Workflow

`.github/workflows/on-demand-backfill.yml` — cron 0/6/12/18 + `workflow_dispatch`.

Reemplaza `ethos-enrich` (deprecated). Schema claim 8183: `gsa-supabase-schema` `20260807010000_bsc_erc_8183_satellite_backfill_claim.sql`.

## Docs

- Repo: [docs/PROCESSES.md](../../docs/PROCESSES.md) proceso #13
- Vault: `12 - Github Worker/On Demand Backfill/`
- ADR: `08 - Decisiones/2026-08-06 - Worker on-demand backfill unificado`
