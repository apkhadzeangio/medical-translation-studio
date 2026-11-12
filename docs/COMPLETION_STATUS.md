# Original plan: implementation and verification

This document distinguishes code support from a completed production pilot. It does not
certify a whole book, the live LLM provider, or all original roadmap acceptance criteria.

## Implemented in the review-workspace change

- Human bilingual editor with debounced autosave, paging, flagged filtering and history.
- Expected-version checks and database row locks prevent lost edits, including an auditor
  waiting for a human correction. Editing invalidates old QA and schedules the new snapshot.
- Human approval, audited QA decisions and approved translation-memory entries.
- Final DOCX/PDF export gate; ordinary review exports remain available.
- Optional academic Georgian refinement after medical review, with a separate model setting,
  versioned prompt, cost-ledger stage, preserved prior text and deterministic edit checks.
- Project pause/resume/cancel, budget editing, failure details, glossary editing and TM import.
- DOCX ingestion for body text, headings, lists, merged tables, raster figures and inline markup.
  Unsupported structures are reported; affected translation is blocked unless explicitly forced.
- Migration `0005`, including refinement progress, QA-resolution audit fields and prompt versions.

Existing PDF ingestion, translation, medical review, QA, cost reporting and exports remain.
No parser expansion is required for ordinary editing work.

## Verification

Use an isolated PostgreSQL database: the test fixture drops the test database's public schema.

```sh
TEST_DATABASE_URL=postgresql://mts:mts@localhost:5432/mts_test .venv/bin/pytest -q
.venv/bin/ruff check app tests migrations
node --check app/web/editor.js
```

Tests cover four-stage orchestration with synthetic providers, rejected numeric changes,
malformed stage output, simultaneous editors, an auditor waiting for a human revision,
QA invalidation, approval/TM, export gating, job controls and DOCX round trips.
Chromium verification also exercised autosave, approval, conflict preservation and mobile layout
with synthetic text, without real model calls.
Result for this change: **128 tests passed**, Ruff passed, JavaScript syntax passed,
and the Python wheel built with the editor script included.

## Deployment and live-provider boundary

This branch has not changed production variables, replaced keys, or deployed to Railway.
The existing Railway API and PostgreSQL were online when inspected. Applying this code
requires `alembic upgrade head` before the new process starts; Railway already has that
pre-deploy command. Back up the database and `/data` volume before a production migration.

GluAPI's public site advertises the OpenAI-compatible base URL `https://api.gluapi.com/v1`.
A model-discovery request from the development environment returned HTTP 403 with
`error code: 1010`. Credential validity, available models, usage accounting, quota and actual
GluAPI charges remain unverified. No paid translation was attempted and no supplied key is
stored in this repository. Resolve the provider's access restriction before a live pilot.
The existing OpenAI token-price configuration does not establish GluAPI's package pricing.

`LLM_REFINE_MODEL` and `LLM_QA_MODEL` must be configured with verified model identifiers to
enable the full model pipeline. A different QA model name alone does not prove vendor or
model independence. Human approval remains the final publishing step.

## Remaining original-plan work

| Item | Status / evidence still needed |
| --- | --- |
| Small real-source pilot | Requires the authorized sample and reachable, verified provider; compare translations and exports with the source |
| Per-key expiry and provider package quotas | Not implemented; current key pool supports round robin, rejection and cooldown |
| Strict concurrent spend reservation | Not implemented; the existing ledger budget check is a pre-call threshold and can overshoot by in-flight requests |
| Full authentication and per-user permissions | Shared API token only; operator labels are audit annotations, not authenticated identities |
| Fully versioned glossary/prohibited terms | Editable glossary exists; those additional policies are not implemented |
| Recovery and backup drill | Existing job recovery tests pass; restoring production database and assets into a separate environment is unverified |
| Large-book and whole-volume acceptance | Requires the source document and pilot; no whole-book completeness or export-layout claim |
| OCR and complex DOCX content | Still unsupported; flagged material requires source review |
| Figure-label replacement | Original figures retained with translated legends for PDFs; in-image replacement is not implemented |
| n8n authoring connection | Setup instructions prepared; this chat has no authenticated n8n MCP/API connection |

These items must not be presented as completed solely because the UI is usable or synthetic
tests pass. The next release should target a verified chapter pilot and spend reservations
before processing an entire book.
