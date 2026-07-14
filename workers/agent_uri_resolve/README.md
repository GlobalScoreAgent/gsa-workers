# Agent URI resolve

> [AGENTS.md](../../AGENTS.md) · [PROCESSES.md](../../docs/PROCESSES.md)

GHA worker that replaces Edge `agent-uri-batch-processor` / `feedback-uri-batch-processor` / `agent-process-uri` for ingest:

1. Claim **agents** first (`is_uri_processed = false`), then **feedbacks** external
2. Resolve URI (HEX / RAW JSON / DATA / IPFS free-first / HTTP → Playwright → deckard_http → Scrape.do)
3. Nested + DID: each child URI is its own `uri_documents` row (lookup before fetch)
4. Upsert `uri_documents` by `uri_hash=md5(uri)` + `agent_manifest` (`uri_document_id` FK; no `data`/`url` columns)
5. Mark source processed

Requires schema migrations:
- `00000000000065_uri_documents_manifest_fk.sql`
- `00000000000066_uri_documents_uri_hash_drop_manifest_dupes.sql`
- `00000000000067_rf_pending_uri_resolve_index.sql` (partial index for feedback claims; agents use existing `idx_agents_pending_uri_processing`)

Claim predicates match those partial indexes (`is_*_processed = false`, not `IS DISTINCT FROM TRUE`).


## Schedule

~every 5.5h UTC + `workflow_dispatch`. Empty queues → exit 0. Soft `MAX_RUNTIME_SECONDS=19800`.

## Env

| Variable | Required | Default |
|---|---|---|
| `SUPABASE_DB_URL` | Yes | — |
| `PINATA_GATEWAY` | No | — (paid IPFS last resort) |
| `SCRAPE_DO_TOKEN` | No | — (last HTTP fallback) |
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
