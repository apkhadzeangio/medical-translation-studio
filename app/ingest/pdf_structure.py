"""Optional model step for complex pages: the model decides structure, never text.

The model sees the page image plus a list of chunk ids with a text preview and returns
only ids: their reading order, their types, and which ones are page furniture. The
answer is accepted only if it uses every id exactly once, so nothing can be lost,
duplicated or invented. Otherwise the deterministic order stands.
"""
from __future__ import annotations

import base64
import uuid

import pymupdf

from app.ingest.pdf_layout import Chunk, PageInfo
from app.llm.gateway import LLMGateway

ALLOWED_TYPES = {"heading", "paragraph", "list_item", "caption", "footnote", "reference", "other"}
PREVIEW_CHARS = 160
# found from the page geometry; the model may move these but not rename them
STRUCTURAL_TYPES = {"table", "figure", "callout", "list", "equation", "chapter_number"}

SYSTEM = """You analyse the layout of one page of an English medical textbook.
You receive the page image and a JSON list of text chunks already extracted from it, each with
an id, a text preview, its bounding box [x0, y0, x1, y1] in points, and a guessed type.

Return ONLY JSON:
{"order": ["id", ...], "types": {"id": "type", ...}, "drop": ["id", ...]}

- "order": the ids in correct human reading order (columns top to bottom, left to right;
  boxes, captions and footnotes after the paragraph they interrupt).
- "drop": ids that are page furniture only: running headers, footers, page numbers, watermarks.
- Every id must appear exactly once, either in "order" or in "drop". Never invent ids.
- "types": only for text ids whose guessed type is wrong. Allowed: heading, paragraph, list_item,
  caption, footnote, reference, other. Tables and figures keep their type.
- Place a table or figure next to its caption, where a reader would look at it.
- Do not return any chunk text."""


class StructureError(ValueError):
    pass


def _preview(c: Chunk) -> str:
    if c.type == "table":
        cells = " | ".join(cell for cell in (c.rows or [[]])[0] if cell)
        return f"[table, {len(c.rows or [])} rows] {cells}"[:PREVIEW_CHARS]
    if c.type == "figure":
        return f"[figure] {' / '.join(c.labels)}"[:PREVIEW_CHARS]
    return c.text[:PREVIEW_CHARS]


def validate(answer: object, ids: list[str]) -> tuple[list[str], dict[str, str], list[str]]:
    if not isinstance(answer, dict):
        raise StructureError("answer is not an object")
    order, drop, types = answer.get("order"), answer.get("drop", []), answer.get("types", {})
    if not isinstance(order, list) or not isinstance(drop, list) or not isinstance(types, dict):
        raise StructureError("wrong field types")
    used = order + drop
    if len(used) != len(set(map(str, used))):
        raise StructureError("an id is used more than once")
    if set(used) != set(ids):
        missing, extra = set(ids) - set(used), set(used) - set(ids)
        raise StructureError(f"ids do not match (missing {sorted(missing)[:5]}, unknown {sorted(extra)[:5]})")
    bad = {k: v for k, v in types.items() if k not in ids or v not in ALLOWED_TYPES}
    if bad:
        raise StructureError(f"invalid types: {bad}")
    if len(drop) > max(4, len(ids) // 3):
        raise StructureError("refusing to drop that much of a page")
    return order, types, drop


def make_reorder(gateway: LLMGateway, project_id: uuid.UUID | None = None, *, dpi: int = 96,
                 model: str | None = None):
    """Build the `reorder` callback for pdf_layout.analyse()."""

    def reorder(page: pymupdf.Page, info: PageInfo, chunks: list[Chunk]) -> list[Chunk] | None:
        png = page.get_pixmap(dpi=dpi).tobytes("png")
        listing = [
            {"id": c.id, "text": _preview(c), "bbox": [round(v) for v in c.bbox], "type": c.type}
            for c in chunks
        ]
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": f"Page {info.number}, {round(info.width)}x{round(info.height)} pt.\n"
                                         f"Chunks: {listing}"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}},
            ]},
        ]
        result = gateway.complete("pdf_structure", messages, project_id=project_id, model=model)
        order, types, _ = validate(result.data, [c.id for c in chunks])
        by_id = {c.id: c for c in chunks}
        out = [by_id[i] for i in order]
        for chunk_id, kind in types.items():
            if chunk_id in order and by_id[chunk_id].type not in STRUCTURAL_TYPES:
                by_id[chunk_id].type = kind
                by_id[chunk_id].type_locked = True
        return out

    return reorder
