# Medical Translation Studio — PR-1 spike

One chapter in, bilingual DOCX out. No UI, no database.

Pipeline: ingest (DOCX/PDF) → translate (glossary + academic Georgian) → medical review
(edits only) → deterministic QA (numbers, glossary, untranslated, length) → export.

## Run

    pip install -r requirements.txt
    export MTS_API_KEYS="sk-..."            # one key, or several comma-separated (round-robin)
    export MTS_BASE_URL="https://api.openai.com/v1"   # or any OpenAI-compatible endpoint
    export MTS_MODEL="gpt-6-astra"
    python spike.py chapter.docx --out out --glossary glossary.csv --max-budget 25

Plumbing test without API calls: `python spike.py tests/sample.docx --out out --mock`

Optional env: `MTS_REVIEW_MODEL`, `MTS_PRICE_IN` / `MTS_PRICE_OUT` (USD per 1M tokens,
default 10 / 50), `MTS_FONT` (default Sylfaen).

## Output (in --out)

- `<name>_bilingual.docx` — English | ქართული | QA flags, one row per segment
- `<name>_ka.docx` — Georgian only, headings and tables kept
- `report.json` — tokens, cost, cost per 1000 words, review edits, QA flags
- `cache/` — per-chunk results; re-running the same command resumes and never re-bills finished chunks

## Known limits of the spike

- Images are not carried over; figure captions are translated as ordinary paragraphs.
- PDF ingestion is text-only and best effort (tables become text, two-column pages may misorder). Prefer DOCX/EPUB-derived input.
- Bibliography entries are detected heuristically and left untranslated.
- The review pass uses the same model unless `MTS_REVIEW_MODEL` is set.
- Only translate material you have the right to translate.
