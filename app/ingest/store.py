"""Persist a parsed PDF as chapters, blocks, segments and assets."""
from __future__ import annotations

import hashlib
import re

import pymupdf
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.ingest.pdf_layout import ParsedDocument
from app.ingest.pdf_objects import render_region
from app.models import Asset, Block, Chapter, Document, Segment
from app.storage import Storage

BLOCK_TYPES = {"heading", "paragraph", "list_item", "caption", "footnote", "reference", "table", "figure"}


def source_hash(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text).strip().lower().encode()).hexdigest()


def _bbox(b) -> dict[str, float]:
    return {"x0": round(b[0], 1), "y0": round(b[1], 1), "x1": round(b[2], 1), "y1": round(b[3], 1)}


def asset_prefix(document: Document) -> str:
    return f"documents/{document.id}/assets"


def store_parsed(
    session: Session,
    document: Document,
    parsed: ParsedDocument,
    *,
    pdf: pymupdf.Document | None = None,
    storage: Storage | None = None,
    replace: bool = False,
) -> dict:
    """Write the parse result.

    With `pdf` and `storage`, every figure and table is also rendered to a PNG asset.
    Refuses to overwrite an already-parsed document unless replace=True.
    """
    existing = session.execute(
        select(func.count()).select_from(Block).where(Block.document_id == document.id)
    ).scalar_one()
    if existing and not replace:
        raise ValueError(f"document {document.id} already has {existing} blocks; pass replace=True")
    if existing:
        session.execute(delete(Block).where(Block.document_id == document.id))
        session.execute(delete(Chapter).where(Chapter.document_id == document.id))
        session.execute(delete(Asset).where(Asset.document_id == document.id))
        if storage is not None:
            storage.delete_prefix(asset_prefix(document))

    chapters = []
    for idx, span in enumerate(parsed.chapters):
        chapter = Chapter(document_id=document.id, idx=idx, title=span.title,
                          page_start=span.page_start, page_end=span.page_end)
        session.add(chapter)
        chapters.append(chapter)
    session.flush()

    render = pdf is not None and storage is not None
    block_idx = {chunk.id: idx for idx, chunk in enumerate(parsed.chunks)}
    boundaries = [span.first_chunk for span in parsed.chapters]
    current = -1
    stats = {"chapters": len(chapters), "blocks": len(parsed.chunks), "segments": 0, "assets": 0}

    for idx, chunk in enumerate(parsed.chunks):
        while current + 1 < len(boundaries) and idx >= boundaries[current + 1]:
            current += 1
        chapter_id = chapters[current].id if current >= 0 else None
        data: dict = {"parts": [{"page": p, "bbox": _bbox(b)} for p, b in chunk.parts]}
        asset_id = None

        if chunk.type in ("table", "figure"):
            data = {"detection": chunk.detection, "caption_block_idx": block_idx.get(chunk.caption_id)}
            if chunk.type == "table":
                data |= {"rows": chunk.rows, "cols": max(len(r) for r in chunk.rows)}
            else:
                data |= {"labels": chunk.labels}
            if render:
                png, width, height = render_region(pdf[chunk.page - 1], chunk.bbox)
                key = f"{asset_prefix(document)}/{chunk.id}.png"
                storage.put(key, png)
                asset = Asset(document_id=document.id, kind="figure" if chunk.type == "figure" else "table_image",
                              page_no=chunk.page, bbox=_bbox(chunk.bbox), storage_key=key, mime="image/png",
                              width=width, height=height)
                session.add(asset)
                session.flush()
                asset_id = asset.id
                stats["assets"] += 1
        else:
            data |= {"font_size": round(chunk.size, 1), "bold": chunk.bold}

        block = Block(
            document_id=document.id, chapter_id=chapter_id, idx=idx,
            type=chunk.type if chunk.type in BLOCK_TYPES else "other",
            level=chunk.level, page_no=chunk.page, bbox=_bbox(chunk.bbox), asset_id=asset_id, data=data,
        )
        session.add(block)
        session.flush()

        if chunk.type == "table":
            cols = data["cols"]
            for r, row in enumerate(chunk.rows):
                for c, cell in enumerate(row):
                    if cell:                 # segment idx encodes the cell: row * cols + col
                        session.add(Segment(block_id=block.id, chapter_id=chapter_id, idx=r * cols + c,
                                            source_text=cell, source_hash=source_hash(cell)))
                        stats["segments"] += 1
        elif chunk.type != "figure":
            session.add(Segment(
                block_id=block.id, chapter_id=chapter_id, idx=0, source_text=chunk.text,
                source_hash=source_hash(chunk.text), translatable=chunk.type != "reference",
            ))
            stats["segments"] += 1

    document.page_count = len(parsed.pages)
    document.status = "parsed"
    document.meta = {
        **(document.meta or {}),
        "parse": {
            "body_font_size": parsed.body_size, "dropped_running_heads": parsed.dropped,
            "tables": sum(p.tables for p in parsed.pages), "figures": sum(p.figures for p in parsed.pages),
            "complex_pages": [p.number for p in parsed.pages if p.complex],
            "scanned_pages": [p.number for p in parsed.pages if p.scanned],
            "columns": {str(p.number): p.columns for p in parsed.pages if p.columns != 1},
            "warnings": parsed.warnings,
        },
    }
    session.flush()
    return stats
