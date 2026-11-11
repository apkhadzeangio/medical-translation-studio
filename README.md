# Medical Translation Studio

Automated translation of English medical textbooks into Georgian: PDF-first ingestion,
multi-stage LLM pipeline (translate → medical review → QA), glossary and translation
memory, cost control, bilingual DOCX/PDF export.

Status: **PR-4b — tables and figures.** Foundation, LLM gateway, and a universal PDF parser.
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

## PDF ingestion

`app/ingest/` turns a text PDF into typed blocks in reading order.

- **Text is never rewritten.** Characters come from the PDF text layer; the model only decides structure.
- Deterministic pass: running headers/footers and page numbers removed, 1..n columns and
  full-width blocks ordered, headings / captions / footnotes / references / list items
  classified, paragraphs joined across column and page breaks, chapters from the PDF outline
  or `CHAPTER n` headings.
- Optional model pass on complex pages only (images, captions, 3+ columns): the model gets the
  page image and chunk ids and returns order, types and page furniture. The answer is used only
  if every id appears exactly once; otherwise the deterministic result stands.
- Superscripts are kept as `<sup>12</sup>` so citation markers do not fuse with words.

- **Tables** become one block with exact cell text (one segment per cell). Ruled grids and
  horizontal-rule ("booktabs") tables under a `Table N` caption are recognised.
- **Figures** (raster images and vector drawings) become one block; text inside the figure is
  kept as `labels`, out of the main flow. Tables and figures are linked to their captions and
  rendered to PNG assets (`STORAGE_DIR`) for the editor and for export.

Inspect a parse without the database:

    python -m app.ingest.cli book.pdf --pages 40-45 --text

Known limits: tables with no rules at all are not recognised yet and stay as paragraphs;
merged cells are not modelled; figure labels are not translated (the image is reused as is);
scanned pages are detected and reported but not OCR'd; a line-break hyphen is removed when the
next line starts lowercase, which also joins true compounds broken at the hyphen
(`follow-` / `up` → `followup`).
PyMuPDF is AGPL-licensed: fine for internal use, needs a commercial licence if this is ever
distributed as closed-source software.

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
