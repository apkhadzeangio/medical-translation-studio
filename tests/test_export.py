import hashlib
import io
import shutil
from pathlib import Path

import pymupdf
import pytest
from docx import Document as open_docx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.export.docx import export_docx
from app.export.pdf import docx_to_pdf
from app.ingest.pdf_layout import analyse
from app.ingest.store import store_parsed
from app.models import Block, Chapter, Document, Project, QAFlag, Segment, SegmentVersion
from app.storage import LocalStorage, get_storage

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_vascular_chapter.pdf"
UNTRANSLATED = "Continuation rules"          # one heading deliberately left without a translation


@pytest.fixture(scope="module")
def book(migrated_db, tmp_path_factory):
    """The fixture chapter with a stand-in Georgian text on every segment but one."""
    storage = LocalStorage(tmp_path_factory.mktemp("assets"))
    data = FIXTURE.read_bytes()
    with Session(migrated_db) as s, pymupdf.open(stream=data, filetype="pdf") as pdf:
        project = Project(name="export")
        s.add(project)
        s.flush()
        doc = Document(project_id=project.id, filename="Synthetic chapter.pdf", format="pdf", storage_key="k",
                       sha256=hashlib.sha256(data + str(project.id).encode()).hexdigest())
        s.add(doc)
        s.flush()
        store_parsed(s, doc, analyse(pdf), pdf=pdf, storage=storage)
        chapter_id = s.scalars(select(Chapter.id).where(Chapter.document_id == doc.id)).first()
        for seg in s.scalars(select(Segment).where(Segment.chapter_id == chapter_id, Segment.translatable)):
            if seg.source_text == UNTRANSLATED:
                continue
            version = SegmentVersion(segment_id=seg.id, stage="translate", text="ქართ " + seg.source_text,
                                     is_current=True)
            s.add(version)
            seg.status = "translated"
            if seg.source_text == "Grouped records":
                s.flush()
                seg.status = "flagged"
                s.add(QAFlag(segment_id=seg.id, version_id=version.id, check_name="qa:terminology",
                             severity="critical", message="ტერმინი გადასამოწმებელია"))
        s.commit()
        return migrated_db, doc.id, chapter_id, storage


def body_text(doc) -> str:
    return "\n".join(p.text for p in doc.paragraphs)


def test_georgian_book_layout(book):
    engine, _, chapter_id, storage = book
    with Session(engine) as s:
        doc = open_docx(io.BytesIO(export_docx(s, [chapter_id], mode="ka", storage=storage)))
    headings = [(p.style.name, p.text) for p in doc.paragraphs if p.style.name.startswith("Heading")]
    assert headings[0] == ("Heading 1", "17. ქართ Infrainguinal Reconstruction: Assessment and Reporting")
    assert ("Heading 2", "ქართ 17.1 PRINCIPLES OF A STRUCTURED RECORD") in headings
    assert ("Heading 3", "ქართ Anatomic description") in headings
    text = body_text(doc)
    assert "SYNTHETIC PARSER FIXTURE" not in text and "SECTION V" not in text        # no page furniture
    assert "ქართ A reconstruction record combines" in text
    assert "(continued)" not in text              # a joined table needs no second caption
    assert len(doc.inline_shapes) == 2                                                # the two figures, not the tables

    # callouts are single-cell tables; data tables follow
    tables = doc.tables
    callouts = [t for t in tables if len(t.columns) == 1]
    # a figure's labels are listed under it: English | Georgian
    legends = [t for t in tables if len(t.columns) == 2
               and "Extraction complete?" in [row.cells[0].text for row in t.rows]]
    assert len(legends) == 1
    assert "ქართ Extraction complete?" in [row.cells[1].text for row in legends[0].rows]
    data = [t for t in tables if len(t.columns) > 1 and t not in legends]
    assert len(callouts) == 3 and callouts[0].cell(0, 0).paragraphs[0].text == "ქართ KEY CONCEPTS"
    assert len(callouts[0].cell(0, 0).paragraphs) == 4
    assert [(len(t.rows), len(t.columns)) for t in data] == [(2 + 18, 8), (9, 2), (7, 2)]

    joined = data[0]
    ids = [joined.cell(r, 1).text for r in range(2, 20)]
    assert ids == [f"ქართ T{n:03d}" for n in range(1, 19)]                            # 18 cases, header printed once
    assert joined.cell(0, 0).text == joined.cell(0, 2).text == "ქართ Case identity"   # merged header group
    assert joined.cell(2, 0)._tc is joined.cell(4, 0)._tc                             # group label spans 3 rows
    assert joined.cell(11, 0)._tc is joined.cell(13, 0)._tc                           # ...also in the continuation
    assert joined.cell(11, 0).text == "ქართ D"


def test_markup_untranslated_and_untouched_blocks(book):
    engine, _, chapter_id, storage = book
    with Session(engine) as s:
        doc = open_docx(io.BytesIO(export_docx(s, [chapter_id], mode="ka", storage=storage)))
    runs = [r for p in doc.paragraphs for r in p.runs]
    cell_runs = [r for t in doc.tables for row in t.rows for c in row.cells for p in c.paragraphs for r in p.runs]
    assert any(r.font.superscript and r.text == "1,2" for r in runs)                  # citation marker
    assert any(r.font.subscript and r.text == "out" for r in runs)                    # Q_out
    assert any(r.font.superscript and r.text == "3" for r in cell_runs)               # 5 × 10^3 in a table cell
    assert "<sup>" not in body_text(doc) and "<sub>" not in body_text(doc)

    gap = next(p for p in doc.paragraphs if p.text == UNTRANSLATED)
    assert gap.runs[0].font.highlight_color is not None                               # visible gap, in English
    done = next(p for p in doc.paragraphs if p.text == "ქართ Grouped records")
    assert done.runs[0].font.highlight_color is None

    text = body_text(doc)
    assert "1. Fixture Editorial Group. Structured observations" in text              # references as printed
    equation = next(p for p in doc.paragraphs if p.text.startswith("Dtest ="))
    assert equation.text == "Dtest = (Qout / Qin) × 100%" and equation.runs[0].font.highlight_color is None
    fonts = {r.font.name for r in runs if r.text.strip()}
    assert fonts == {"Sylfaen"}


def test_bilingual_review_table(book):
    engine, _, chapter_id, _ = book
    with Session(engine) as s:
        doc = open_docx(io.BytesIO(export_docx(s, [chapter_id], mode="bilingual")))
        segments = s.scalar(select(Segment.id).join(Block).where(Block.chapter_id == chapter_id).limit(1)) is not None
        count = len(list(s.scalars(select(Segment.id).where(Segment.chapter_id == chapter_id))))
    assert segments
    table = doc.tables[0]
    assert [c.text for c in table.rows[0].cells] == ["გვ.", "English", "ქართული", "QA"]
    rows = {row.cells[1].text: row for row in table.rows[1:]}
    assert len(table.rows) == 1 + count + 2                                           # segments + 2 figure rows
    flagged = rows["Grouped records"]
    assert flagged.cells[2].text == "ქართ Grouped records"
    assert flagged.cells[3].text == "[critical] ტერმინი გადასამოწმებელია"
    assert "FDE9E7" in flagged.cells[0]._tc.xml                                       # flagged rows are tinted
    assert rows[UNTRANSLATED].cells[2].text == ""                                     # nothing invented for a gap
    assert rows["0.05 / 0.5 / 5.0"].cells[0].text == "5"                              # source page number
    assert doc.sections[0].page_width > doc.sections[0].page_height


def test_export_api_docx_and_errors(book, client):
    engine, document_id, chapter_id, storage = book
    client.app.dependency_overrides[get_storage] = lambda: storage
    response = client.get(f"/api/chapters/{chapter_id}/export")
    assert response.status_code == 200 and response.content[:2] == b"PK"
    assert 'filename="chapter_1_ka.docx"' in response.headers["content-disposition"]
    whole = client.get(f"/api/documents/{document_id}/export", params={"mode": "bilingual"})
    assert whole.status_code == 200 and "Synthetic_chapter_bilingual.docx" in whole.headers["content-disposition"]
    assert client.get(f"/api/chapters/{chapter_id}/export", params={"format": "epub"}).status_code == 422
    assert client.get("/api/chapters/00000000-0000-0000-0000-000000000000/export").status_code == 404


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice not installed")
def test_pdf_export_keeps_georgian_text(book):
    engine, _, chapter_id, storage = book
    with Session(engine) as s:
        pdf = docx_to_pdf(export_docx(s, [chapter_id], mode="ka", storage=storage))
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        text = "".join(page.get_text() for page in doc)
        images = sum(len(page.get_images()) for page in doc)
    assert pdf.startswith(b"%PDF") and "ქართ" in text and "�" not in text
    assert "T018" in text and images >= 2
