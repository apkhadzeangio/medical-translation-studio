# Medical Translation Studio

Automated translation of English medical textbooks into Georgian: PDF-first ingestion,
multi-stage LLM pipeline (translate → medical review → QA), glossary and translation
memory, cost control, bilingual DOCX/PDF export.

Status: **PR-8 — translation stage.** A parsed chapter can be translated end to end; so far only against a simulated model.
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
| POST / GET | `/api/projects/{id}/documents` | upload a PDF (queued for parsing) / list |
| GET | `/api/documents/{id}` | status, chapters, block and segment counts, parse report |
| POST | `/api/documents/{id}/reparse` | parse again, replacing the previous result |
| GET | `/api/projects/{id}/jobs` | job counts by kind and status |
| POST | `/api/projects/{id}/jobs/retry-failed` | requeue failed jobs |
| GET / POST | `/api/projects/{id}/glossary` | search terms / add or change one |
| POST | `/api/projects/{id}/glossary/import` | CSV or TSV: English term, Georgian term, optional note |
| GET | `/api/projects/{id}/glossary/export` | glossary as CSV |
| DELETE | `/api/glossary/{term_id}` | remove a term |
| POST | `/api/projects/{id}/tm/import` | CSV or TSV of translated sentence pairs |
| GET | `/api/projects/{id}/tm` | translation-memory entry counts by origin |
| POST | `/api/chapters/{id}/translate` | queue a chapter; returns chunks, words and a rough cost estimate |
| POST | `/api/documents/{id}/translate` | queue every chapter of a parsed document |
| GET | `/api/chapters/{id}/segments` | source and translation side by side with open QA flags |
| GET | `/api/projects/{id}/cost` | budget, amount spent, tokens and cost per stage |

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

## Translation stage

`app/pipeline/translate.py` turns a chapter into `translate_chunk` jobs (about
`TRANSLATE_CHUNK_WORDS` source words each). For each chunk:

1. segments already translated are skipped, so a retry pays only for what is missing
2. identical source text found in the translation memory is reused without a model call
3. the rest goes to the model with the chapter title, the glossary terms that occur in the
   chunk, and the preceding text as context
4. an answer that does not cover every segment is rejected whole and the job retries
5. every translation is checked without a model (`app/pipeline/checks.py`): numbers,
   `<sup>`/`<sub>` markup, inequality and unit symbols, untranslated text, length, glossary.
   A critical finding marks the segment `flagged` and keeps it out of the translation memory.

References, equations and the chapter numeral are not sent for translation.

## Glossary and translation memory

`app/terminology.py`:

- **Glossary.** All of a project's terms compile into one pattern: whole words only, the
  longest term wins (`abdominal aortic aneurysm` over `aneurysm`), plurals match. `find()`
  gives the terms present in a chunk (these go into the translation prompt); `violations()`
  lists terms whose Georgian equivalent is missing from a translation, comparing word stems
  so that case endings do not raise false alarms.
- **Translation memory.** Exact reuse of an earlier translation of the same source text
  (whitespace-insensitive, case-sensitive). An entry approved by a person is never overwritten
  by a machine translation. Fuzzy matching is not implemented.

## Background jobs

`app/jobs.py` is a Postgres queue (`FOR UPDATE SKIP LOCKED`), `app/worker.py` runs it:

    python -m app.worker

- enqueueing is idempotent on the job key, so planning twice never duplicates work
- each job runs in one transaction; a failure rolls back and retries with backoff (5 attempts)
- a job left running by a dead worker is requeued after 30 minutes
- a job that hits the budget cap is paused for an hour without spending an attempt
- `app/chunking.py` groups a chapter's segments into translation chunks without splitting a
  table, list or callout, and without leaving a heading at the end of a chunk

While storage is a local directory the worker must see the same disk as the API: set
`EMBEDDED_WORKER=true` to run it inside the API process (single Railway service with a volume).

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

- **Tables**: rows from horizontal rules and shaded row bands, columns from text alignment, cell
  text read from the text layer unchanged (one segment per cell). Merged header groups and
  group labels are recorded as spans; a table continued on the next page is linked to its
  first fragment and its repeated header rows are marked.
- **Figures** (raster images and vector drawings) become one block; text inside the figure is
  kept as `labels`, out of the main flow. Tables and figures are linked to their captions and
  notes and rendered to PNG assets (`STORAGE_DIR`) for the editor and for export.
- **Callouts** (shaded or framed boxes), **lists**, **equations** and the chapter numeral are
  separate block kinds. Sub- and superscripts are kept as `<sub>` / `<sup>`.

Inspect a parse, or score it against a ground-truth file:

    python -m app.ingest.cli book.pdf --pages 40-45 --text
    python -m app.ingest.evaluate book.pdf truth.json --verbose

`tests/fixtures/synthetic_vascular_chapter.pdf` with its ground truth is the regression
fixture: the parser must keep scoring 100% on its six text pages.

Known limits: scanned pages are detected and reported but not OCR'd (out of scope); tables
with neither rules nor shading stay as paragraphs; figure labels are not translated (the
image is reused as is); a line-break hyphen is removed when the next line starts lowercase,
which would also join a true compound broken exactly at its hyphen (`follow-` / `up`).
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
