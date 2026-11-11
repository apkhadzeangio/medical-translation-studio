"""Persist a parsed PDF as chapters, blocks and segments."""
from __future__ import annotations

import hashlib
import re

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.ingest.pdf_layout import ParsedDocument
from app.models import Block, Chapter, Document, Segment

TYPE_MAP = {"heading": "heading", "paragraph": "paragraph", "list_item": "list_item", "caption": "caption",
            "footnote": "footnote", "reference": "reference", "other": "other"}


def source_hash(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text).strip().lower().encode()).hexdigest()


def _bbox(b) -> dict[str, float]:
    return {"x0": round(b[0], 1), "y0": round(b[1], 1), "x1": round(b[2], 1), "y1": round(b[3], 1)}


def store_parsed(session: Session, document: Document, parsed: ParsedDocument, *, replace: bool = False) -> dict:
    """Write the parse result. Refuses to overwrite an already-parsed document unless replace=True."""
    existing = session.execute(
        select(func.count()).select_from(Block).where(Block.document_id == document.id)
    ).scalar_one()
    if existing and not replace:
        raise ValueError(f"document {document.id} already has {existing} blocks; pass replace=True")
    if existing:
        session.execute(delete(Block).where(Block.document_id == document.id))
        session.execute(delete(Chapter).where(Chapter.document_id == document.id))

    chapters = []
    for idx, span in enumerate(parsed.chapters):
        chapter = Chapter(document_id=document.id, idx=idx, title=span.title,
                          page_start=span.page_start, page_end=span.page_end)
        session.add(chapter)
        chapters.append(chapter)
    session.flush()

    boundaries = [span.first_chunk for span in parsed.chapters]
    current = -1
    segments = 0
    for idx, chunk in enumerate(parsed.chunks):
        while current + 1 < len(boundaries) and idx >= boundaries[current + 1]:
            current += 1
        chapter_id = chapters[current].id if current >= 0 else None
        block = Block(
            document_id=document.id, chapter_id=chapter_id, idx=idx, type=TYPE_MAP.get(chunk.type, "other"),
            level=chunk.level, page_no=chunk.page, bbox=_bbox(chunk.bbox),
            data={"font_size": round(chunk.size, 1), "bold": chunk.bold,
                  "parts": [{"page": p, "bbox": _bbox(b)} for p, b in chunk.parts]},
        )
        session.add(block)
        session.flush()
        session.add(Segment(
            block_id=block.id, chapter_id=chapter_id, idx=0, source_text=chunk.text,
            source_hash=source_hash(chunk.text), translatable=chunk.type != "reference",
        ))
        segments += 1

    document.page_count = len(parsed.pages)
    document.status = "parsed"
    document.meta = {
        **(document.meta or {}),
        "parse": {
            "body_font_size": parsed.body_size, "dropped_running_heads": parsed.dropped,
            "complex_pages": [p.number for p in parsed.pages if p.complex],
            "scanned_pages": [p.number for p in parsed.pages if p.scanned],
            "columns": {str(p.number): p.columns for p in parsed.pages if p.columns != 1},
            "warnings": parsed.warnings,
        },
    }
    session.flush()
    return {"chapters": len(chapters), "blocks": len(parsed.chunks), "segments": segments}
