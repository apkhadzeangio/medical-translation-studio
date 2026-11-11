# Medical Translation Studio

Automated translation of English medical textbooks into Georgian: PDF-first ingestion,
multi-stage LLM pipeline (translate → medical review → QA), glossary and translation
memory, cost control, bilingual DOCX/PDF export.

Status: **PR-3 — LLM gateway.** Foundation (API, schema, migrations, CI) plus the provider gateway.
The earlier one-file pilot lives in `tools/spike/`.

## Local development

    docker compose up -d db
    python -m venv .venv && . .venv/bin/activate
    pip install -e ".[dev]"
    cp .env.example .env
    alembic upgrade head
    uvicorn app.main:app --reload      # http://localhost:8000/docs
    pytest -q

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness, no database access |
| GET | `/health/db` | readiness: database reachable and migrated |
| POST / GET | `/api/projects` | create / list projects |
| GET | `/api/projects/{id}` | one project |

`/api/*` requires `Authorization: Bearer $API_TOKEN` when `API_TOKEN` is set.

## Schema

| Table | Role |
|---|---|
| `projects`, `documents`, `chapters` | what is being translated |
| `blocks` | source structure in reading order, with `page_no` and `bbox` |
| `assets` | figures and page renders extracted from the PDF |
| `segments` | translatable units (paragraph, heading, caption, table cell) |
| `segment_versions` | every stage's output; exactly one `is_current` per segment |
| `qa_flags` | deterministic and model QA findings |
| `glossary_terms`, `tm_entries` | terminology and translation memory |
| `jobs` | Postgres work queue with idempotency keys |
| `llm_calls` | token and cost ledger per request |

## LLM gateway

`app/llm/gateway.py` is the only code that calls a model provider (any OpenAI-compatible
endpoint, configured with `LLM_*` variables — see `.env.example`).

- several API keys used round-robin; a rate-limited key rests, a rejected key is dropped
- retries with backoff on 429 / 5xx / timeouts, honouring `Retry-After`
- hard stop when the project budget (`projects.budget_usd`) or `LLM_BUDGET_USD` is reached
- every attempt is written to `llm_calls` with tokens and cost; keys are never stored
- a model without a configured price is refused, so spend is never untracked

## Railway

One service from this repo plus the Postgres plugin. Set `DATABASE_URL=${{Postgres.DATABASE_URL}}`
and `API_TOKEN`. `railway.toml` runs `alembic upgrade head` before each deploy and uses
`/health/db` as the health check.

## Migrations

    alembic revision --autogenerate -m "describe change"
    alembic upgrade head

`tests/test_schema.py` fails if models and migrations drift apart.
