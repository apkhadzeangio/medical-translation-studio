import io
import uuid

import pymupdf
from docx import Document as new_docx
from docx.shared import Inches
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.ingest.docx import paragraph_text
from app.main import create_app
from app.models import Asset, Block, Chapter, Job, Segment
from app.storage import LocalStorage, get_storage
from app.worker import Worker, default_handlers


def make_document():
    document = new_docx()
    document.add_heading("Chapter 1: Synthetic dose", 1)
    paragraph = document.add_paragraph("Give 5 mg.")
    paragraph.add_run("12").font.superscript = True
    table = document.add_table(rows=3, cols=2)
    table.cell(0, 0).merge(table.cell(0, 1)).text = "Dose comparison"
    table.cell(1, 0).text, table.cell(1, 1).text = "Control", "5 mg"
    table.cell(2, 0).text, table.cell(2, 1).text = "Treatment", "10 mg"
    image = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 80))
    image.clear_with(220)
    document.add_picture(io.BytesIO(image.tobytes("png")), width=Inches(1))
    document.add_paragraph("Figure 1: Synthetic chart", "Caption")
    document.add_heading("Chapter 2: Follow-up", 1)
    document.add_paragraph("No occlusion was observed.", "List Bullet")
    data = io.BytesIO()
    document.save(data)
    return data.getvalue()


def test_docx_upload_preserves_body_order_merged_cells_images_and_markup(migrated_db, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    with Session(migrated_db) as session:
        session.execute(update(Job).where(Job.status.in_(("queued", "running"))).values(status="cancelled"))
        session.commit()
    storage = LocalStorage(tmp_path)
    monkeypatch.setattr("app.ingest.job.get_storage", lambda: storage)
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app) as client:
        project_id = client.post("/api/projects", json={"name": "DOCX test"}).json()["id"]
        url = f"/api/projects/{project_id}/documents"
        data = make_document()
        document = client.post(url, files={"file": ("chapter.docx", data)}).json()
        assert document["format"] == "docx" and document["status"] == "uploaded"
        assert client.post(url, files={"file": ("same.docx", data)}).json()["id"] == document["id"]
        assert Worker(migrated_db, default_handlers()).run_once() == "done"
        detail = client.get(f"/api/documents/{document['id']}").json()
        assert detail["status"] == "parsed" and detail["page_count"] is None
        assert len(detail["chapters"]) == 2 and detail["parse"]["blocking"] == 0
        exported = client.get(f"/api/documents/{document['id']}/export")
        assert exported.status_code == 200
        output = new_docx(io.BytesIO(exported.content))
        assert len(output.inline_shapes) == 1 and len(output.tables) == 1
        assert "5 mg" in output.tables[0].cell(1, 1).text
        assert client.post(url, files={"file": ("fake.docx", b"not a document")}).status_code == 415
    with Session(migrated_db) as session:
        blocks = list(session.scalars(select(Block).where(
            Block.document_id == uuid.UUID(document["id"])).order_by(Block.idx)))
        assert [block.type for block in blocks] == [
            "heading", "paragraph", "table", "figure", "caption", "heading", "list_item"]
        assert blocks[2].data["spans"] == [{"row_start": 0, "row_end": 0, "col_start": 0, "col_end": 1}]
        assert session.scalar(select(Segment.source_text).where(
            Segment.block_id == blocks[1].id)) == "Give 5 mg.<sup>12</sup>"
        assert len(list(session.scalars(select(Asset).where(Asset.document_id == uuid.UUID(document["id"]))))) == 1
        assert len(list(session.scalars(select(Chapter).where(Chapter.document_id == uuid.UUID(document["id"]))))) == 2


def test_docx_hyperlink_text_is_not_lost():
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    document = new_docx()
    paragraph = document.add_paragraph("See ")
    hyperlink = OxmlElement("w:hyperlink")
    run, text = OxmlElement("w:r"), OxmlElement("w:t")
    text.text = "the reference"
    run.append(text)
    hyperlink.append(run)
    hyperlink.set(qn("w:anchor"), "ref1")
    paragraph._p.append(hyperlink)
    assert paragraph_text(paragraph) == "See the reference"
