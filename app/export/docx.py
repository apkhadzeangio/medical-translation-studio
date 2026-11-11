"""DOCX export of translated chapters.

Two layouts:
  "ka"        the Georgian book: headings, paragraphs, lists, callouts, tables with merged
              cells, figures from the source PDF, captions, notes, references
  "bilingual" a review table, one row per segment: English | Georgian | QA findings

A segment that has no translation yet is printed in English on a yellow background, so a
gap is never mistaken for finished text. References and equations are printed as in the source.
"""
from __future__ import annotations

import io
import re
import uuid
from dataclasses import dataclass, field

from docx import Document as new_docx
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Pt, RGBColor
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Asset, Block, Chapter, QAFlag, Segment, SegmentVersion
from app.storage import Storage

MARKUP = re.compile(r"<(su[pb])>(.*?)</\1>")
SHADE_HEADER, SHADE_CALLOUT, SHADE_FLAGGED = "E7EEF7", "F3F3F3", "FDE9E7"


@dataclass
class Cell:
    """What to print for one segment."""

    text: str
    translated: bool
    source: str = ""
    status: str = ""
    flags: list[str] = field(default_factory=list)


@dataclass
class Item:
    block: Block
    cells: dict[int, Cell]          # segment idx -> content


def load_items(session: Session, chapter_ids: list[uuid.UUID]) -> list[Item]:
    blocks = list(session.scalars(
        select(Block).where(Block.chapter_id.in_(chapter_ids)).order_by(Block.document_id, Block.idx)))
    by_block: dict[uuid.UUID, dict[int, Cell]] = {b.id: {} for b in blocks}
    rows = session.execute(
        select(Segment, SegmentVersion.text)
        .outerjoin(SegmentVersion, (SegmentVersion.segment_id == Segment.id) & SegmentVersion.is_current)
        .where(Segment.block_id.in_(list(by_block)))).all()
    flags: dict[uuid.UUID, list[str]] = {}
    for flag in session.scalars(select(QAFlag).where(
            QAFlag.segment_id.in_([seg.id for seg, _ in rows]), ~QAFlag.resolved)):
        flags.setdefault(flag.segment_id, []).append(f"[{flag.severity}] {flag.message}")
    for seg, text in rows:
        done = text is not None or not seg.translatable
        by_block[seg.block_id][seg.idx] = Cell(text=text if text is not None else seg.source_text, translated=done,
                                               source=seg.source_text, status=seg.status,
                                               flags=flags.get(seg.id, []))
    return [Item(b, by_block[b.id]) for b in blocks]


# ----------------------------------------------------------------------------- low-level helpers


def _font(run, name: str, size: float | None = None) -> None:
    run.font.name = name
    fonts = run._element.get_or_add_rPr().get_or_add_rFonts()
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):       # Georgian is a complex script for Word
        fonts.set(qn(attr), name)
    if size:
        run.font.size = Pt(size)


def add_text(paragraph, text: str, font: str, *, size: float | None = None, bold: bool = False,
             italic: bool = False, highlight: bool = False) -> None:
    """Write text, turning <sup>/<sub> markup into real superscript and subscript runs."""
    position = 0
    pieces: list[tuple[str, str | None]] = []
    for match in MARKUP.finditer(text):
        pieces.append((text[position:match.start()], None))
        pieces.append((match.group(2), match.group(1)))
        position = match.end()
    pieces.append((text[position:], None))
    for piece, kind in pieces:
        if not piece:
            continue
        run = paragraph.add_run(piece)
        _font(run, font, size)
        run.bold, run.italic = bold, italic
        if kind == "sup":                 # both properties share one XML element: set only one
            run.font.superscript = True
        elif kind == "sub":
            run.font.subscript = True
        if highlight:
            run.font.highlight_color = WD_COLOR_INDEX.YELLOW


def _shade(cell, color: str) -> None:
    properties = cell._element.get_or_add_tcPr()
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), color)
    properties.append(shading)


def _usable_width(doc) -> int:
    section = doc.sections[0]
    return section.page_width - section.left_margin - section.right_margin


# ----------------------------------------------------------------------------- Georgian book


def _logical_tables(items: list[Item]) -> tuple[dict[int, list[Item]], set[int]]:
    """Group a table with the fragments that continue it; list blocks to leave out of the flow."""
    by_idx = {item.block.idx: item for item in items}
    groups: dict[int, list[Item]] = {}
    skip: set[int] = set()
    for item in items:
        if item.block.type != "table":
            continue
        head = item.block.data.get("continues_block_idx")
        if head is not None and head in by_idx:
            groups.setdefault(head, [by_idx[head]]).append(item)
            skip.add(item.block.idx)
            caption = item.block.data.get("caption_block_idx")
            if caption is not None:
                skip.add(caption)                    # "Table 17.1 (continued)" is not needed once joined
        else:
            groups.setdefault(item.block.idx, [item])
    return groups, skip


def _table_rows(item: Item, drop: int = 0) -> tuple[list[list[Cell | None]], list[dict]]:
    data = item.block.data
    cols = data["cols"]
    rows = [[item.cells.get(r * cols + c) for c in range(cols)] for r in range(len(data["rows"]))]
    spans = [s for s in data.get("spans", []) if s["row_start"] >= drop]
    return rows[drop:], [{**s, "row_start": s["row_start"] - drop, "row_end": s["row_end"] - drop} for s in spans]


def _write_table(doc, fragments: list[Item], font: str) -> None:
    head = fragments[0]
    rows, spans = _table_rows(head)
    for fragment in fragments[1:]:
        more, more_spans = _table_rows(fragment, fragment.block.data.get("repeated_header_rows", 0))
        offset = len(rows)
        spans += [{**s, "row_start": s["row_start"] + offset, "row_end": s["row_end"] + offset} for s in more_spans]
        rows += more
    cols = head.block.data["cols"]
    header_rows = head.block.data.get("header_rows", 0)
    table = doc.add_table(rows=len(rows), cols=cols)
    table.style = "Table Grid"
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            target = table.cell(r, c)
            if r < header_rows:
                _shade(target, SHADE_HEADER)
            if cell is not None:
                add_text(target.paragraphs[0], cell.text, font, size=9, bold=r < header_rows,
                         highlight=not cell.translated)
    for span in spans:
        if span["row_end"] < len(rows) and span["col_end"] < cols:
            table.cell(span["row_start"], span["col_start"]).merge(table.cell(span["row_end"], span["col_end"]))
    doc.add_paragraph()


def _write_figure(doc, session: Session, item: Item, storage: Storage | None, font: str) -> None:
    asset = session.get(Asset, item.block.asset_id) if item.block.asset_id else None
    paragraph = doc.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if asset is None or storage is None:
        add_text(paragraph, "[სურათი ვერ მოიძებნა]", font, italic=True, highlight=True)
        return
    width = _usable_width(doc)
    if asset.width and asset.height:                 # never upscale a small figure to full width
        natural = Emu(int(asset.width / 200 * 914400))   # assets are rendered at 200 dpi
        width = min(width, natural)
    paragraph.add_run().add_picture(io.BytesIO(storage.get(asset.storage_key)), width=width)


def write_book(session: Session, items: list[Item], storage: Storage | None, font: str):
    doc = new_docx()
    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = font, Pt(11)
    tables, skip = _logical_tables(items)
    chapter_number = ""
    for item in items:
        block, kind = item.block, item.block.type
        if block.idx in skip:
            continue
        cells = [item.cells[i] for i in sorted(item.cells)]
        if kind == "table":
            _write_table(doc, tables[block.idx], font)
        elif kind == "figure":
            _write_figure(doc, session, item, storage, font)
        elif not cells:
            continue
        elif kind == "other" and block.data.get("role") == "chapter_number":
            chapter_number = cells[0].text.strip()
        elif kind == "heading":
            level = min(block.level or 2, 4)
            heading = doc.add_heading(level=level)
            prefix = f"{chapter_number}. " if chapter_number and level == 1 else ""
            chapter_number = "" if level == 1 else chapter_number
            add_text(heading, prefix + cells[0].text, font, highlight=not cells[0].translated)
            for run in heading.runs:
                run.font.color.rgb = RGBColor(0x1F, 0x2A, 0x44)
        elif kind == "callout":
            box = doc.add_table(rows=1, cols=1)
            box.style = "Table Grid"
            _shade(box.cell(0, 0), SHADE_CALLOUT)
            for n, cell in enumerate(cells):
                paragraph = box.cell(0, 0).paragraphs[0] if n == 0 else box.cell(0, 0).add_paragraph()
                add_text(paragraph, cell.text, font, size=10, bold=n == 0, highlight=not cell.translated)
            doc.add_paragraph()
        elif kind in ("list", "list_item"):
            for cell in cells:
                paragraph = doc.add_paragraph()
                paragraph.paragraph_format.left_indent = Pt(18)
                add_text(paragraph, cell.text, font, highlight=not cell.translated)
        elif kind == "caption":
            add_text(doc.add_paragraph(), cells[0].text, font, size=9, italic=True, highlight=not cells[0].translated)
        elif kind in ("footnote", "reference"):
            for cell in cells:
                add_text(doc.add_paragraph(), cell.text, font, size=8.5, highlight=not cell.translated)
        elif kind == "equation":
            paragraph = doc.add_paragraph()
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            add_text(paragraph, cells[0].text, font, italic=True)
        else:
            paragraph = doc.add_paragraph()
            paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            add_text(paragraph, cells[0].text, font, highlight=not cells[0].translated)
    return doc


# ----------------------------------------------------------------------------- bilingual review table


def write_bilingual(session: Session, items: list[Item], font: str):
    doc = new_docx()
    section = doc.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = section.page_height, section.page_width
    doc.styles["Normal"].font.name = font
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    for cell, title in zip(table.rows[0].cells, ("გვ.", "English", "ქართული", "QA"), strict=True):
        _shade(cell, SHADE_HEADER)
        add_text(cell.paragraphs[0], title, font, size=9, bold=True)
    for item in items:
        if item.block.type == "figure":
            row = table.add_row().cells
            add_text(row[0].paragraphs[0], str(item.block.page_no or ""), font, size=8)
            add_text(row[1].paragraphs[0], "[figure]", font, size=9, italic=True)
            continue
        for idx in sorted(item.cells):
            cell = item.cells[idx]
            row = table.add_row().cells
            bold = item.block.type == "heading"
            add_text(row[0].paragraphs[0], str(item.block.page_no or ""), font, size=8)
            add_text(row[1].paragraphs[0], cell.source, font, size=9, bold=bold)
            if cell.translated:
                add_text(row[2].paragraphs[0], cell.text, font, size=9, bold=bold)
            add_text(row[3].paragraphs[0], "\n".join(cell.flags), font, size=7.5)
            if cell.status == "flagged":
                for target in row:
                    _shade(target, SHADE_FLAGGED)
    widths = (0.45, 4.1, 4.1, 1.35)
    for row in table.rows:
        for cell, inches in zip(row.cells, widths, strict=True):
            cell.width = int(inches * 914400)
    return doc


# ----------------------------------------------------------------------------- entry point


def export_docx(session: Session, chapter_ids: list[uuid.UUID], *, mode: str = "ka",
                storage: Storage | None = None) -> bytes:
    if mode not in ("ka", "bilingual"):
        raise ValueError(f"unknown export mode {mode!r}")
    ordered = list(session.scalars(
        select(Chapter.id).where(Chapter.id.in_(chapter_ids)).order_by(Chapter.document_id, Chapter.idx)))
    items = load_items(session, ordered)
    font = get_settings().export_font
    doc = write_bilingual(session, items, font) if mode == "bilingual" else write_book(session, items, storage, font)
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
