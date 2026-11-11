"""Table and figure detection for one PDF page.

Tables
  1. ruled grids: PyMuPDF's line-based table finder
  2. "booktabs" tables (horizontal rules only, the usual textbook style): the region between
     the rules under a `Table N` caption, with columns inferred from text alignment
Cell text is taken from the text layer unchanged.

Figures
  raster images plus clusters of vector drawings (charts, line art), merged when they touch.
  Text lying inside a figure (axis labels, callouts) is kept as `labels`, out of the main flow.

Each table/figure becomes one Chunk; text chunks inside it are removed from the flow.
"""
from __future__ import annotations

import pymupdf

from app.ingest.pdf_layout import CAPTION_RE, LIGATURES, BBox, Chunk, join_lines

MIN_FIGURE_SIDE = 40.0      # points; smaller graphics are icons, bullets, rules
MAX_PAGE_SHARE = 0.85       # a graphic covering the whole page is a background or frame
RULE_THICKNESS = 2.5
MIN_RULE_WIDTH = 80.0


def _area(b: BBox) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _intersection(a: BBox, b: BBox) -> float:
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def _center_in(inner: BBox, outer: BBox, pad: float = 2.0) -> bool:
    cx, cy = (inner[0] + inner[2]) / 2, (inner[1] + inner[3]) / 2
    return outer[0] - pad <= cx <= outer[2] + pad and outer[1] - pad <= cy <= outer[3] + pad


def _cell_text(words: list[tuple], cell: BBox | None) -> str:
    """Exact text-layer words whose centre lies in the cell, in line order.

    PyMuPDF's own table.extract() re-spaces characters ("3 .0 -3 .9 cm"), so cells are
    rebuilt from the page's words instead.
    """
    if cell is None:
        return ""
    lines: dict[tuple[int, int], list[tuple[int, str]]] = {}
    for x0, y0, x1, y1, word, block, line, number in words:
        if cell[0] - 1 <= (x0 + x1) / 2 <= cell[2] + 1 and cell[1] - 1 <= (y0 + y1) / 2 <= cell[3] + 1:
            lines.setdefault((block, line), []).append((number, word.translate(LIGATURES)))
    ordered = [" ".join(w for _, w in sorted(items)) for _, items in sorted(lines.items())]
    return join_lines([ln for ln in ordered if ln.strip()])


def _usable(rows: list[list[str]]) -> bool:
    if len(rows) < 2 or max((len(r) for r in rows), default=0) < 2:
        return False
    filled = sum(1 for r in rows for cell in r if cell)
    return filled >= 3 and sum(1 for r in rows if sum(bool(c) for c in r) >= 2) >= 2


def _extract(table, words: list[tuple]) -> list[list[str]]:
    rows = [[_cell_text(words, cell) for cell in row.cells] for row in table.rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return rows
    keep = [i for i in range(max(len(r) for r in rows)) if any(i < len(r) and r[i] for r in rows)]
    return [[r[i] if i < len(r) else "" for i in keep] for r in rows]


def _horizontal_rules(page: pymupdf.Page) -> list[BBox]:
    rules = []
    for d in page.get_drawings():
        r = d["rect"]
        if r.height <= RULE_THICKNESS and r.width >= MIN_RULE_WIDTH:
            rules.append((r.x0, r.y0, r.x1, r.y1))
    return sorted(rules, key=lambda b: b[1])


def _grid_tables(page: pymupdf.Page) -> list:
    try:
        return list(page.find_tables().tables)
    except Exception:  # noqa: BLE001 - the table finder is best effort on odd PDFs
        return []


def find_tables(page: pymupdf.Page, number: int, chunks: list[Chunk]) -> list[Chunk]:
    found: list[Chunk] = []
    words = page.get_text("words")

    def add(bbox: BBox, rows: list[list[str]], how: str) -> None:
        if not _usable(rows) or _area(bbox) > MAX_PAGE_SHARE * abs(page.rect):
            return
        if any(_intersection(bbox, t.bbox) > 0.5 * min(_area(bbox), _area(t.bbox)) for t in found):
            return
        found.append(Chunk(id=f"p{number}t{len(found)}", page=number, bbox=bbox, text="", size=0.0,
                           bold=False, lines=len(rows), type="table", rows=rows, detection=how))

    for t in _grid_tables(page):
        add(tuple(t.bbox), _extract(t, words), "grid")

    captions = [c for c in chunks if c.text.lower().startswith("table") and CAPTION_RE.match(c.text)]
    rules = _horizontal_rules(page) if captions else []
    for cap in captions:
        if any(abs(t.bbox[1] - cap.bbox[3]) < 60 or abs(cap.bbox[1] - t.bbox[3]) < 60 for t in found):
            continue
        below = [r for r in rules if r[1] >= cap.bbox[3] - 3
                 and min(r[2], cap.bbox[2]) - max(r[0], cap.bbox[0]) > 0.3 * (cap.bbox[2] - cap.bbox[0])]
        run: list[BBox] = []
        for r in below:
            if not run and r[1] - cap.bbox[3] > 40:
                break
            if run and r[1] - run[-1][1] > 320:
                break
            run.append(r)
        if len(run) < 2:
            continue
        region = (min(r[0] for r in run) - 2, run[0][1] - 1, max(r[2] for r in run) + 2, run[-1][3] + 1)
        try:
            tabs = page.find_tables(clip=pymupdf.Rect(region), horizontal_strategy="text", vertical_strategy="text")
        except Exception:  # noqa: BLE001
            continue
        for t in tabs.tables:
            add(region, _extract(t, words), "rules")
            break
    return found


def find_figures(page: pymupdf.Page, number: int, chunks: list[Chunk], image_boxes: list[BBox],
                 tables: list[Chunk]) -> list[Chunk]:
    page_area = abs(page.rect)
    boxes: list[BBox] = [b for b in image_boxes
                         if b[2] - b[0] >= MIN_FIGURE_SIDE and b[3] - b[1] >= MIN_FIGURE_SIDE]
    detection = {b: "image" for b in boxes}

    try:
        drawings = page.get_drawings()
        clusters = page.cluster_drawings(drawings=drawings) if drawings else []
    except Exception:  # noqa: BLE001
        drawings, clusters = [], []
    for rect in clusters:
        box = (rect.x0, rect.y0, rect.x1, rect.y1)
        if rect.width < MIN_FIGURE_SIDE or rect.height < MIN_FIGURE_SIDE:
            continue
        if any(_intersection(box, t.bbox) > 0.3 * _area(box) for t in tables):
            continue
        parts = sum(1 for d in drawings if rect.contains(d["rect"]))
        prose = sum(len(c.text) for c in chunks if _center_in(c.bbox, box))
        if parts < 4 and prose > 200:
            continue                    # a shaded text box, not a figure
        boxes.append(box)
        detection[box] = "vector"

    boxes = [b for b in boxes if _area(b) <= MAX_PAGE_SHARE * page_area]
    merged: list[tuple[BBox, str]] = []
    for box in sorted(boxes, key=_area, reverse=True):
        for i, (m, how) in enumerate(merged):
            grown = (m[0] - 6, m[1] - 6, m[2] + 6, m[3] + 6)
            if _intersection(box, grown) > 0:
                merged[i] = ((min(m[0], box[0]), min(m[1], box[1]), max(m[2], box[2]), max(m[3], box[3])),
                             how if how == detection[box] else "mixed")
                break
        else:
            merged.append((box, detection[box]))
    merged.sort(key=lambda item: (item[0][1], item[0][0]))
    return [Chunk(id=f"p{number}f{i}", page=number, bbox=box, text="", size=0.0, bold=False, lines=1,
                  type="figure", detection=how) for i, (box, how) in enumerate(merged)]


def detect_objects(page: pymupdf.Page, number: int, chunks: list[Chunk], image_boxes: list[BBox]) -> list[Chunk]:
    """Return the page's chunks with tables and figures added and their inner text removed."""
    tables = find_tables(page, number, chunks)
    figures = find_figures(page, number, chunks, image_boxes, tables)
    if not tables and not figures:
        return chunks
    kept: list[Chunk] = []
    for c in chunks:
        if CAPTION_RE.match(c.text):
            kept.append(c)
        elif any(_center_in(c.bbox, t.bbox) for t in tables):
            continue                    # the text lives in table.rows
        else:
            owner = next((f for f in figures if _center_in(c.bbox, f.bbox, pad=0.0)), None)
            if owner is not None and len(c.text) <= 200:
                owner.labels.append(c.text)
            else:
                kept.append(c)
    return kept + tables + figures


def render_region(page: pymupdf.Page, bbox: BBox, dpi: int = 200) -> tuple[bytes, int, int]:
    """PNG of a page region (figure or table), with everything drawn inside it."""
    clip = pymupdf.Rect(bbox) & page.rect
    pix = page.get_pixmap(clip=clip, dpi=dpi, alpha=False)
    return pix.tobytes("png"), pix.width, pix.height
