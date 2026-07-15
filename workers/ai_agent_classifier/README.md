# AI Agent Classifier

> Project context: [AGENTS.md](../../AGENTS.md) · [Supabase map](../../docs/SUPABASE.md) · [Architecture](../../docs/ARCHITECTURE.md) · [Processes](../../docs/PROCESSES.md)

Claim worker that classifies `web_dashboard.agents` into service categories using OpenAI-compatible LLMs configured in schema `llm` (`process_code=agent-classifier`).

## Eligibility

```sql
does_need_ai_category_process IS TRUE
```

Another process sets the flag to `TRUE`. This worker sets it `FALSE` on success or error.

## Pipeline

1. Load active categories from `web_dashboard.agent_ai_categories`
2. Load active providers/models for `llm.process.process_code = 'agent-classifier'`
3. Claim agents with `FOR UPDATE SKIP LOCKED` (no soft-lock columns)
4. Pick a model with remaining daily capacity (`models_requests` vs `request_per_day`)
5. Call `{base_url}/chat/completions` with provider params (`temperature`, `max_completion_tokens`, `response_format`)
6. Upsert `llm.models_requests` (`request_total += 1` for `CURRENT_DATE`)
7. On success: write `ai_category_*`, `llm_model_id`, clear error cols, flag `FALSE`
8. On failure: `has_ai_category_process_error=TRUE`, `ai_category_process_error_message`, flag `FALSE`

Exit `0` when: queue empty, all models hit daily limit, or `MAX_RUNTIME_SECONDS` reached.

API keys come from GitHub Secrets / env vars named by `llm.llm_provider.secret` (today: `GROQ`). Endpoint from `llm.llm_provider.base_url`.

## Environment

| Variable | Default | Description |
|---|---|---|
| `SUPABASE_DB_URL` | required | Postgres connection string |
| `GROQ` | required (for Groq) | Groq API key (`llm.llm_provider.secret`) |
| `CLAIM_BATCH_SIZE` | 20 | Agents claimed per loop |
| `CONCURRENCY` | 1 | Parallel LLM calls (max 5; keep low for rpm) |
| `MAX_RUNTIME_SECONDS` | 19800 | Soft stop (~5.5h) |

## Monitoring

```sql
SELECT
  count(*) FILTER (WHERE does_need_ai_category_process IS TRUE) AS pending,
  count(*) FILTER (WHERE has_ai_category_process_error IS TRUE) AS errors,
  count(*) FILTER (WHERE ai_category_primary IS NOT NULL) AS classified
FROM web_dashboard.agents;

SELECT m.name, mr.date, mr.request_total, m.request_per_day
FROM llm.models_requests mr
JOIN llm.models m ON m.id = mr.model_id
WHERE mr.date = CURRENT_DATE
ORDER BY m.id;
```

## Local run

```bash
cd workers/ai_agent_classifier
uv sync
uv run python job.py
```

## Workflow

`.github/workflows/ai-agent-classifier.yml` — cron `0 0,6,12,18 * * *` UTC + `workflow_dispatch`.
