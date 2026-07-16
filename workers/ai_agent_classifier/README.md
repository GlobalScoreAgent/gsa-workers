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
2. Load `llm.process.system_prompt` for `process_code = 'agent-classifier'` (editable in DB)
3. Requeue prior errors (`has_ai_category_process_error`) back onto the claim queue
4. Load active providers/models for that process
5. Claim agents with `FOR UPDATE SKIP LOCKED` (no soft-lock columns)
6. Fingerprint prompt inputs (`ai_category_input_hash`); if another agent already classified the same inputs, **copy** categories and skip the LLM
7. Else pick a model with remaining daily capacity (`request_total` / `token_total` vs day caps)
8. Call `{base_url}/chat/completions` with provider params (`temperature`, `max_completion_tokens`, `response_format`); if `llm.models.does_need_thinking_off_parameter` then also `reasoning_effort=none` + `clear_thinking=false`
9. Upsert `llm.models_requests` (`request_total += 1`, `token_total += usage.total_tokens`) — not incremented on copy
10. On success: write `ai_category_*`, `ai_category_input_hash`, `llm_model_id`, clear error cols, flag `FALSE`
11. On failure: `has_ai_category_process_error=TRUE`, `ai_category_process_error_message`, flag `FALSE`

Allowlist includes quality categories `Invalid Metadata` / `Insufficient Metadata` and `Trading Bots` (Ave/Debot-style clones) vs distinct `Trading`. Prompt rules live in `llm.process.system_prompt`.

Exit `0` when: queue empty, all models hit daily request/token limits, or `MAX_RUNTIME_SECONDS` reached.

Rate limits: sliding-window hardcaps use `request_per_minute` and `tokens_per_minute`. Daily caps use `request_per_day` and `tokents_per_day` (column name as in DB). HTTP 429 TPM is retried; 429 TPD skips that model for the rest of the run.

API keys come from GitHub Secrets / env vars named by `llm.llm_provider.secret` (today: `GROQ`). Endpoint from `llm.llm_provider.base_url`.

## Environment

| Variable | Default | Description |
|---|---|---|
| `SUPABASE_DB_URL` | required | Postgres connection string |
| `GROQ` | required (for Groq) | Groq API key (`llm.llm_provider.secret`) |
| `CEREBRAS` | required (for Cerebras) | Cerebras API key (`llm.llm_provider.secret`) |
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

SELECT m.name, mr.date, mr.request_total, mr.token_total,
       m.request_per_day, m.tokents_per_day
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

After deploying `ai_category_input_hash`, backfill classified donors once:

```bash
uv run python backfill_input_hash.py
```

## Workflow

`.github/workflows/ai-agent-classifier.yml` — cron `0 0,6,12,18 * * *` UTC + `workflow_dispatch`.
