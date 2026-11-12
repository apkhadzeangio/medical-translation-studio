# Medical Translation Studio

Automated translation of English medical textbooks into Georgian: PDF-first ingestion,
multi-stage LLM pipeline (translate → medical review → QA), glossary and translation
memory, cost control, bilingual DOCX/PDF export.

Status: **live on Railway.** First real run (2026-11-12, gpt-6-astra): a 1,879-word chapter translated and reviewed in about 2 minutes for $0.65, i.e. about $0.35 per 1,000 source words without the QA stage.
The earlier one-file pilot lives in `tools/spike/`.

## Review workspace

Open a chapter's **ორენოვანი რედაქტორი** to edit the Georgian translation beside its source.
Changes autosave as human revisions; **თარგმანის დამტკიცება** is a separate action and adds
the approved pair to translation memory. The editor keeps your draft when another operator
has changed the version. Old QA is invalidated on edits and a configured auditor is requeued.
Critical findings require a correction or an explicit, recorded human explanation before approval.

**სამუშაო DOCX/PDF** exports remain available for review. **საბოლოო DOCX/PDF** requires every
translatable segment to be approved and every critical finding to be resolved. Download the
QA report for counts and source-linked findings. Readiness and final-file generation share
segment locks so a concurrent edit cannot slip into an approved export.

Project controls pause, resume or cancel queued processing without deleting translations.
Requests already in flight finish. Resume restores work cancelled by the operator and releases
budget-paused jobs, while preserving ordinary retry backoff. Job details display recent failures.
The console also supports editing the budget, searching/editing glossary terms, and TM imports.

`LLM_REFINE_MODEL` enables the separate Georgian academic-editing stage:

`translate → medical review → academic refinement → QA`

It is unset by default for compatibility with existing deployments. `LLM_QA_MODEL` is still
required for model QA. The console explicitly identifies either unconfigured stage. Prompt
versions are recorded in the model-call ledger. Synthetic-provider tests exercise all four
stages; they do not establish the quality or availability of a live provider.

See [completion status](docs/COMPLETION_STATUS.md) for outstanding plan items and
[the n8n connection guide](docs/N8N_CONNECTION.md) for connecting an AI coding client.

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
| GET | `/health/db` | readiness: database reachable and migrated to the revision this code needs |
| GET | `/health/worker` | embedded worker threads alive, queue counts, number of model keys (never the keys) |
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
| POST | `/api/chapters/{id}/translate` | queue a chapter; returns chunks, words and a rough cost estimate. 409 if parsing reported damaged structure on its pages (`?force=true` to translate anyway) |
| POST | `/api/documents/{id}/translate` | queue every chapter of a parsed document |
| POST | `/api/chapters/{id}/review` | queue the medical review (normally automatic) |
| POST | `/api/chapters/{id}/qa` | queue the independent QA (needs `LLM_QA_MODEL`) |
| GET | `/api/chapters/{id}/segments` | source and translation side by side with open QA flags |
| GET | `/api/segments/{id}/versions` | every stage's text and every QA finding for one segment |
| PUT | `/api/segments/{id}/translation` | version-checked human edit (`text`, `expected_version_id`, optional `note`) |
| POST | `/api/segments/{id}/approve` | human approval (`expected_version_id`) |
| POST | `/api/qa-flags/{id}/resolve` | recorded QA decision (`expected_version_id`, `note`) |
| POST | `/api/chapters/{id}/refine` | queue academic Georgian editing (`LLM_REFINE_MODEL`) |
| GET | `/api/chapters/{id}/quality` | source-linked publication-readiness report |
| GET | `/api/documents/{id}/quality` | whole-document readiness report |
| POST | `/api/projects/{id}/processing/{pause\|resume\|cancel}` | control job claims; in-flight work finishes |
| GET | `/api/projects/{id}/jobs/details` | latest 100 jobs, attempts and errors |
| PATCH | `/api/projects/{id}` | change name or budget |
| GET | `/api/runtime` | configured models/provider and key count; no secrets |
| GET | `/api/projects/{id}/cost` | budget, amount spent, tokens and cost per stage |
| GET | `/api/chapters/{id}/export?format=docx\|pdf&mode=ka\|bilingual` | one chapter as a file |
| GET | `/api/documents/{id}/export?format=…&mode=…` | all chapters in one file |

`/api/*` requires `Authorization: Bearer $API_TOKEN` when `API_TOKEN` is set.
Add `final=true` to either export endpoint to enforce the approval gate. Without it,
exports are drafts and can contain unapproved or untranslated source material.

## DOCX ingestion

The upload endpoint accepts DOCX as well as PDF. DOCX ingestion preserves body order,
heading levels, lists, editable tables with merged cells, embedded raster images,
hyperlink wording and superscript/subscript runs. Original files are kept. Word documents
have no stable page numbers before pagination, so the importer does not invent them.

Footnotes/endnotes, equations, text boxes, nested tables and pictures inside table cells
currently require source review and are reported as blocking structural issues. Figure
labels remain in the source language and are reported as warnings. Expanded DOCX packages
are limited to 256 MB and 10,000 entries. This is a supported subset, not lossless handling
of every Word feature. A reparse of a document with translations now requires `force=true`.

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

## Export

`app/export/`:

- **`mode=ka`** — the Georgian book: headings by level, justified paragraphs, lists, callouts as
  shaded boxes, tables with merged cells, figures taken from the source PDF, captions, notes.
  A table continued over several pages is joined into one, with its header printed once.
  References and equations are printed as in the source. `<sup>`/`<sub>` become real
  superscript and subscript.
- **`mode=bilingual`** — a landscape review table, one row per segment: page, English, Georgian,
  open QA findings; flagged rows are tinted.
- A segment with no translation yet is printed in English on a yellow background (and left
  empty in the bilingual table), so a gap is never mistaken for finished text.
- **PDF** is the DOCX converted by LibreOffice, so both always show the same content. The
  Docker image installs LibreOffice and Georgian-capable fonts; without it the API answers 501.

`EXPORT_FONT` (default Sylfaen) is the font named in the DOCX. Export is generated on request;
a whole book will take a while and will move to a background job with the web console.

## Review and QA

`app/pipeline/review.py`. After each chunk job an `advance_chapter` job checks whether the
chapter's stage is complete and queues the next one:

`translating` → `reviewing` → `qa` → `done`

- **Medical review** (`LLM_REVIEW_MODEL`, default `LLM_MODEL`) sees source, translation and the
  problems the automatic checks already found, and returns edits only. An edit that would break
  a number, a symbol or the markup is refused and the earlier text stands. An accepted edit
  becomes a new version; the earlier one is kept.
- **Independent QA** (`LLM_QA_MODEL`) reports problems and never changes text. It gets no hints
  from the earlier stages. Use a model from another vendor: the plan response says
  `"independent": false` when it is the same model as the reviewer. Without `LLM_QA_MODEL` the
  stage is skipped and only the automatic checks apply.
- A chapter with an open critical finding stays in `qa` until a person resolves it.

Set `{"auto_review": false}` or `{"auto_qa": false}` in a project's settings to run a stage by hand.

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
  kept as `labels` with the place each one is printed (`label_boxes`), out of the main flow.
  A label that contains a word is a segment and is translated; the export prints the labels
  under the picture in both languages. Tables and figures are linked to their captions and
  notes and rendered to PNG assets (`STORAGE_DIR`) for the editor and for export.
- **Framed panels**: a frame that holds captions in shaded bars (one page-wide frame with
  several figures and tables, as in "at a glance" handbooks) is split at the bars. Under a bar,
  a table is a group of shaded bands with no artwork in it; everything else drawn there is
  that caption's figure. Labels around a drawing are never read as table cells.
- **Fraction formulas** (a short rule with a centred line above and below, and `= X` beside
  it) leave the text flow as one `equation` block: rendered as an image, parts stored in
  `data.formula`, not translated, and always listed in the warnings.
- **Lists inside a paragraph block**: a lead-in sentence, its bullets or numbered steps
  (`7 Inflate cuff …`) and the paragraph after them are separate blocks; a list that runs on
  in the next column or block is one list.
- **Running feet** that change with every chapter are dropped when they carry the page number.
- **Callouts** (shaded or framed boxes), **lists**, **equations** and the chapter numeral are
  separate block kinds. Sub- and superscripts are kept as `<sub>` / `<sup>`.

**Structure check.** After parsing, `review_structure` records what the parser is unsure
about in `documents.meta.parse.issues` (`page`, `code`, `severity`, `message`); the console
lists them on the document card.

| severity | codes | effect |
|---|---|---|
| `error` | `reading_order`, `table_structure`, `table_not_found`, `stray_symbols` | the structure is known to be damaged: `POST …/translate` answers 409 with the issues; `?force=true` overrides, and the console asks before it does |
| `warning` | `formula_image`, `figure_labels`, `figure_without_caption`, `figure_not_found`, `table_without_caption`, `scanned` | translation is allowed; a person should look at the page |

Inspect a parse, or score it against a ground-truth file:

    python -m app.ingest.cli book.pdf --pages 40-45 --text
    python -m app.ingest.evaluate book.pdf truth.json --verbose

`tests/fixtures/synthetic_vascular_chapter.pdf` with its ground truth is the regression
fixture: the parser must keep scoring 100% on its six text pages.

The page designs that first broke on a real handbook (footer across the gutter, fraction
formula, framed diagram with leader labels, table beside a formula and a chart) are rebuilt as
synthetic pages in `tests/pdf_factory.py` and pinned by `tests/test_pdf_panels.py`. No page of
a copyrighted book belongs in this repository.

Known limits: scanned pages are detected and reported but not OCR'd (out of scope); tables
with neither rules nor shading stay as paragraphs; a figure's image keeps its English labels
(the Georgian ones are in the legend under it); the wording inside a fraction formula is not
translated; two figure labels printed directly under each other at normal line spacing can
only be told apart by a capital letter; a line-break hyphen is removed when the next line starts lowercase,
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

## Console

`/` serves a single static page (`app/web/index.html`, no build step) that drives the API with
the access token: create a project with a budget, upload a PDF, import a glossary, start a
chapter's translation, watch progress and cost, download DOCX/PDF. It is phone-sized first.

## Railway

See [docs/DEPLOY_RAILWAY.md](docs/DEPLOY_RAILWAY.md): one service with a volume at `/data`,
Postgres, and the variables listed there. With `ENVIRONMENT=production` the app will not start
without `API_TOKEN`.

## Migrations

    alembic revision --autogenerate -m "describe change"
    alembic upgrade head

`tests/test_schema.py` fails if models and migrations drift apart.
