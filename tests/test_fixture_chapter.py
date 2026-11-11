"""End-to-end parse of the user's synthetic textbook chapter, scored against its ground truth.

The oracle is read only here. Page 7 of the fixture is a scan and is out of scope (no OCR).
"""
import json
from pathlib import Path

import pymupdf
import pytest

from app.ingest.evaluate import evaluate
from app.ingest.pdf_layout import analyse

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def parsed():
    with pymupdf.open(FIXTURES / "synthetic_vascular_chapter.pdf") as doc:
        return analyse(doc)


@pytest.fixture(scope="module")
def truth():
    return json.loads((FIXTURES / "synthetic_vascular_chapter.truth.json").read_text(encoding="utf-8"))


def test_scores_against_ground_truth(parsed, truth):
    result = evaluate(parsed, truth)
    assert result["issues"] == []
    assert result["overall"] == 100.0


def test_continued_table_is_one_logical_table(parsed, truth):
    first, second = [c for c in parsed.chunks if c.type == "table" and c.label == "17.1"]
    assert second.continues == first.id and second.repeated_header_rows == 2
    body = first.rows[first.header_rows:] + second.rows[second.repeated_header_rows:]
    logical = next(t for t in truth["tables"] if t["number"] == "17.1")
    assert len(body) == truth["expected_counts"]["continued_table_body_rows"]
    assert [row[1] for row in body] == [row[1] for row in logical["body_rows"]]


def test_numeric_and_negation_tokens_survive(parsed):
    text = "\\n".join(c.text for c in parsed.chunks)
    cells = {cell for c in parsed.chunks if c.rows for row in c.rows for cell in row}
    for token in ("≤ 0.80 is not interchangeable with ≥ 0.80", "0.05 is not interchangeable with 0.5",
                  "3 of 12 records (25%)", "no occlusion was recorded is not equivalent to occlusion was recorded",
                  "was not confirmed", "follow-up", "reconstruction break", "finding and flow"):
        assert token in text, token
    for cell in ("0.05 / 0.5 / 5.0", "1,250 / 1.250", "≤ 0.80 / ≥ 1.20", "5 × 10<sup>3</sup>", "37 °C / 12 µm",
                 "8-12 / follow-up", "0 / NR / blank", "not assessed", "not visible", "absent"):
        assert cell in cells, cell


def test_georgian_glossary_is_intact(parsed, truth):
    glossary = next(c for c in parsed.chunks if c.type == "table" and c.label == "17.3")
    want = next(b for b in truth["blocks"] if b.get("logical_table_id") == "table-17-3" and b["type"] == "table")
    assert glossary.rows == want["rows"]
    assert len(glossary.rows) - glossary.header_rows == truth["expected_counts"]["glossary_body_rows"]
    assert "\\ufffd" not in "".join(cell for row in glossary.rows for cell in row)


def test_figures_and_callouts(parsed):
    figures = [c for c in parsed.chunks if c.type == "figure" and c.page <= 6]
    assert [(f.label, f.detection) for f in figures] == [("17.1", "image"), ("17.2", "vector")]
    assert "Extraction complete?" in figures[1].labels
    callouts = [c for c in parsed.chunks if c.type == "callout"]
    assert [c.items[0] for c in callouts] == ["KEY CONCEPTS", "INTERPRETATION NOTE", "CASE VIGNETTE 17.1 - FICTIONAL"]
    assert len(callouts[0].items) == 4


def test_structure_details(parsed):
    assert parsed.chapters[0].title == "17 Infrainguinal Reconstruction: Assessment and Reporting"
    equation = next(c for c in parsed.chunks if c.type == "equation")
    assert equation.text == "D<sub>test</sub> = (Q<sub>out</sub> / Q<sub>in</sub>) × 100%"
    steps = next(c for c in parsed.chunks if c.type == "list")
    assert len(steps.items) == 5 and steps.items[1] == "a. Preserve its printed unit."
    assert sum(c.type == "reference" for c in parsed.chunks) == 8
    assert parsed.pages[6].scanned and parsed.dropped == 24   # 4 furniture runs on each of 6 pages
