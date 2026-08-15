# Agent URI resolve

> [AGENTS.md](../../AGENTS.md) · [PROCESSES.md](../../docs/PROCESSES.md)

GHA worker that replaces Edge `agent-uri-batch-processor` / `feedback-uri-batch-processor` / `agent-process-uri` for ingest, plus on-chain feedback materialize:

1. Claim **agents** (`is_uri_processed = false`) → resolve URI
2. Claim **feedback_on_chain** → upsert `uri_documents` + `agent_manifest` with `source='on_chain'` (**no HTTP fetch**; synthetic URI `internal_on_chain_id_{feedback_id}`)
3. Claim **feedback_uri** / **feedback_end_point** → resolve URI
4. Nested + DID: each child URI is its own `uri_documents` row (lookup before fetch)
5. Upsert `uri_documents` by `uri_hash=md5(uri)` + `agent_manifest` (`uri_document_id` FK; no `data`/`url` columns)

Requires schema migrations:
- `00000000000065_uri_documents_manifest_fk.sql`
- `00000000000066_uri_documents_uri_hash_drop_manifest_dupes.sql`
- `00000000000067_rf_pending_uri_resolve_index.sql` (external feedback claims; agents use `idx_agents_pending_uri_processing`)
- `00000000000068_rf_pending_on_chain_index.sql` (on-chain feedback claims)

Claim predicates match those partial indexes (`is_*_processed = false`).

Loop priority per round: agents → on-chain → external feedbacks. Empty on all three → exit 0.

## Schedule

Twice daily UTC (`00:00`, `12:00`) + `workflow_dispatch`. Soft `MAX_RUNTIME_SECONDS=19800`.
Error-retry / off-chain refresh: sibling worker [`agent_uri_reprocess`](../agent_uri_reprocess/README.md) at `06:00` / `18:00` UTC.

## Env

| Variable | Required | Default |
|---|---|---|
| `SUPABASE_DB_URL` | Yes | — |
| `PINATA_GATEWAY` | No | — (paid IPFS last resort) |
| `SCRAPING_ANT_KEY` | No | — (last HTTP fallback; ScrapingAnt free tier) |
| `CLAIM_BATCH_SIZE` | No | `20` |
| `CLAIM_STALE_SECONDS` | No | `7200` |
| `CONCURRENCY` | No | `4` |
| `MAX_RUNTIME_SECONDS` | No | `19800` |
| `WORKER_ID` | No | `resolve-a` |

## Local

```powershell
cd workers/agent_uri_resolve
uv sync
uv run playwright install chromium
uv run python job.py
```

## Smoke ScrapingAnt

Optional last-fallback provider. Requires `SCRAPING_ANT_KEY` in the environment (GitHub Actions secret or local env; do not commit).

```powershell
cd workers/agent_uri_resolve
uv sync
$env:SCRAPING_ANT_KEY = '...'   # local only
uv run python scripts/smoke_scraping_ant.py
```

Expect `OK` with HTTP 200 and `Ant-credits-cost=10` when `browser=true`. In production, ScrapingAnt only runs after direct fetch, Playwright, and Deckard fail. Successes land in `uri_documents.source_gateway` as `scraping-ant-*`.
