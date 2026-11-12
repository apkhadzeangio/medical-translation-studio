"""DOCX ingestion in body order. Preserve the source; report unsupported structures.

Word has no stable page coordinates before pagination, so DOCX blocks use paragraph
order and no invented page numbers. Embedded pictures keep their original bytes.
"""
import io
import re
import uuid
from zipfile import BadZipFile, ZipFile

from docx import Document as read_docx
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.ingest.store import asset_prefix, source_hash
from app.models import Asset, Block, Chapter, Document, Segment
from app.storage import Storage

MAX_EXPANDED_BYTES = 256 * 1024 * 1024


def validate(data: bytes) -> None:
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            if not {"[Content_Types].xml", "word/document.xml"} <= names:
                raise ValueError("not a DOCX document")
            if len(names) > 10000 or sum(item.file_size for item in archive.infolist()) > MAX_EXPANDED_BYTES:
                raise ValueError("DOCX expanded content exceeds 256 MB or 10000 entries")
    except BadZipFile as exc:
        raise ValueError("invalid DOCX archive") from exc


def paragraph_text(paragraph: Paragraph) -> str:
    parts = []
    for item in paragraph.iter_inner_content():
        runs = item.runs if hasattr(item, "runs") else [item]
        for run in runs:
            value = run.text
            if run.font.superscript:
                value = f"<sup>{value}</sup>"
            elif run.font.subscript:
                value = f"<sub>{value}</sub>"
            parts.append(value)
    return "".join(parts).strip()


def ingest(session: Session, document: Document, data: bytes, storage: Storage) -> None:
    validate(data)
    source = read_docx(io.BytesIO(data))
    session.execute(delete(Block).where(Block.document_id == document.id))
    session.execute(delete(Chapter).where(Chapter.document_id == document.id))
    session.execute(delete(Asset).where(Asset.document_id == document.id))
    storage.delete_prefix(asset_prefix(document))
    chapter = None
    chapter_index = block_index = 0
    issues = []

    def issue(code, message, severity="warning"):
        if not any(item["code"] == code for item in issues):
            issues.append({"page": 0, "code": code, "severity": severity, "message": message})

    def add_block(kind, texts, *, level=None, extra=None, asset_id=None):
        nonlocal chapter, chapter_index, block_index
        if chapter is None or (kind == "heading" and level == 1 and block_index > 0):
            chapter = Chapter(document_id=document.id, idx=chapter_index,
                              title=(texts[0] if kind == "heading" else document.filename)[:500])
            chapter_index += 1
            session.add(chapter)
            session.flush()
        block = Block(document_id=document.id, chapter_id=chapter.id, idx=block_index,
                      type=kind, level=level, data=extra or {}, asset_id=asset_id)
        block_index += 1
        session.add(block)
        session.flush()
        for index, value in enumerate(texts):
            if value:
                session.add(Segment(block_id=block.id, chapter_id=chapter.id, idx=index,
                                    source_text=value, source_hash=source_hash(value)))
        return block

    def pictures(paragraph):
        for blip in paragraph._p.xpath(".//a:blip"):
            reference = blip.get(qn("r:embed"))
            if not reference or reference not in paragraph.part.related_parts:
                issue("docx_external_image", "An external or unavailable image was not imported.", "error")
                continue
            part = paragraph.part.related_parts[reference]
            if part.content_type not in {"image/png", "image/jpeg", "image/tiff", "image/gif", "image/bmp"}:
                issue("docx_image", "An embedded object is not a supported image.", "error")
                continue
            asset = Asset(id=uuid.uuid4(), document_id=document.id, kind="figure", mime=part.content_type,
                          storage_key=f"{asset_prefix(document)}/{uuid.uuid4().hex}")
            storage.put(asset.storage_key, part.blob)
            session.add(asset)
            session.flush()
            add_block("figure", [], asset_id=asset.id)
            issue("docx_figure_labels", "Image labels remain in the source language; check them in the original.")

    for item in source.iter_inner_content():
        if isinstance(item, Paragraph):
            text = paragraph_text(item)
            name = item.style.name if item.style else ""
            heading = re.match(r"Heading (\d+)$", name, re.I)
            kind = "heading" if heading or name == "Title" else "paragraph"
            level = int(heading[1]) if heading else (1 if name == "Title" else None)
            if name == "Caption":
                kind = "caption"
            elif name.startswith("List") or item._p.xpath(".//w:numPr"):
                kind = "list_item"
            if text:
                add_block(kind, [text], level=level)
            pictures(item)
            if item._p.xpath(".//w:footnoteReference|.//w:endnoteReference|.//m:oMath|.//w:txbxContent"):
                issue("docx_complex_content",
                      "Footnotes, equations or text boxes need source review before translation.", "error")
        elif isinstance(item, Table):
            rows, positions = [], {}
            for row_index, row in enumerate(item.rows):
                values = [""] * row.grid_cols_before
                for index, cell in enumerate(row.cells):
                    col_index = index + row.grid_cols_before
                    seen = cell._tc in positions
                    positions.setdefault(cell._tc, []).append((row_index, col_index))
                    values.append("" if seen else "\n".join(paragraph_text(p) for p in cell.paragraphs).strip())
                    if cell.tables:
                        issue("docx_nested_table", "Nested tables need source review before translation.", "error")
                    if cell._tc.xpath(".//w:footnoteReference|.//w:endnoteReference|.//m:oMath|.//w:txbxContent"):
                        issue("docx_complex_cell",
                              "A table contains unsupported notes, equations or text boxes.", "error")
                    for paragraph in cell.paragraphs:
                        if paragraph._p.xpath(".//a:blip"):
                            issue("docx_table_image", "A picture inside a table needs source review.", "error")
                values += [""] * row.grid_cols_after
                rows.append(values)
            cols = max((len(row) for row in rows), default=0)
            if not cols:
                continue
            rows = [row + [""] * (cols - len(row)) for row in rows]
            spans = [{"row_start": min(p[0] for p in spots), "row_end": max(p[0] for p in spots),
                      "col_start": min(p[1] for p in spots), "col_end": max(p[1] for p in spots)}
                     for spots in positions.values() if len(spots) > 1]
            header_rows = 0
            for row in item.rows:
                if not row._tr.xpath(".//w:tblHeader"):
                    break
                header_rows += 1
            add_block("table", [value for row in rows for value in row], extra={
                "rows": rows, "cols": cols, "spans": spans, "header_rows": header_rows})
    if not block_index:
        raise ValueError("DOCX contains no supported body content")
    document.status, document.page_count = "parsed", None
    document.meta = {**(document.meta or {}), "parse": {
        "format": "docx", "issues": issues, "warnings": [item["message"] for item in issues],
        "blocking": sum(item["severity"] == "error" for item in issues)}}
    session.flush()
