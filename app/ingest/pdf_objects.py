"""Table, figure and callout detection for one PDF page.

Tables
  Textbook tables are drawn with horizontal rules and/or shaded row bands, rarely with a full
  grid. Rows come from those horizontal edges; columns come from how the text lines up; cell
  text is read from the text layer inside each cell, unchanged. A booktabs table (three rules)
  falls back to one row per text line. True grids drawn cell by cell use PyMuPDF's strict
  line finder.

Figures
  Raster images plus clusters of vector drawings (charts, flow diagrams), merged when they
  touch. Text inside a figure is kept as `labels`, out of the main flow.

Callouts
  A plain shaded or framed box around running text ("Key concepts", a case vignette) becomes
  one callout chunk whose lines stay translatable.

Each object becomes one Chunk; the text chunks inside it leave the flow.
"""
from __future__ import annotations

import statistics

import pymupdf

from app.ingest.pdf_layout import CAPTION_RE, BBox, Chunk, _line_text, join_lines

MIN_FIGURE_SIDE = 40.0      # points; smaller graphics are icons, bullets, rules
MAX_PAGE_SHARE = 0.85       # a graphic covering the whole page is a background or frame
RULE_THICKNESS = 2.5
MIN_RULE_WIDTH = 80.0
MIN_ROW_HEIGHT = 6.0
MAX_RULE_GAP = 200.0        # rules further apart than this do not belong to one table
FIGURE_MIN_PARTS = 4        # fewer drawing parts than this is a box, not artwork
Edge = tuple[float, float, float]   # x0, x1, y


def _area(b: BBox) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _intersection(a: BBox, b: BBox) -> float:
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def _center_in(inner: BBox, outer: BBox, pad: float = 2.0) -> bool:
    cx, cy = (inner[0] + inner[2]) / 2, (inner[1] + inner[3]) / 2
    return outer[0] - pad <= cx <= outer[2] + pad and outer[1] - pad <= cy <= outer[3] + pad


def _box(rect: pymupdf.Rect) -> BBox:
    return (rect.x0, rect.y0, rect.x1, rect.y1)


# ----------------------------------------------------------------------------- tables


def cell_text(page: pymupdf.Page, box: BBox) -> tuple[str, bool]:
    """Exact text-layer content of a cell (with <sup>/<sub> markup) and whether it is all bold."""
    data = page.get_text("dict", clip=pymupdf.Rect(box),
                         flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_LIGATURES)
    lines, bold = [], []
    for block in data["blocks"]:
        for line in block.get("lines", []):
            text, _, is_bold, _ = _line_text(line)
            if text:
                lines.append(text)
                bold.append(is_bold)
    return join_lines(lines), bool(bold) and all(bold)


def _horizontal_edges(drawings: list[dict]) -> list[Edge]:
    """Rules, and the top and bottom of filled or framed bands."""
    edges: list[Edge] = []
    for d in drawings:
        r = d["rect"]
        if r.width < 20:
            continue
        if r.height <= RULE_THICKNESS:
            edges.append((r.x0, r.x1, (r.y0 + r.y1) / 2))
        elif r.height >= MIN_ROW_HEIGHT:
            edges += [(r.x0, r.x1, r.y0), (r.x0, r.x1, r.y1)]
    return edges


def _rule_stacks(drawings: list[dict]) -> list[BBox]:
    """Regions spanned by three or more rules of the same width (booktabs tables)."""
    groups: dict[tuple[int, int], list[float]] = {}
    for d in drawings:
        r = d["rect"]
        if r.height <= RULE_THICKNESS and r.width >= MIN_RULE_WIDTH:
            groups.setdefault((round(r.x0 / 3), round(r.x1 / 3)), []).append((r.x0, r.x1, (r.y0 + r.y1) / 2))
    regions: list[BBox] = []
    for rules in groups.values():
        rules.sort(key=lambda e: e[2])
        run = [rules[0]]
        for rule in rules[1:] + [None]:
            if rule is not None and rule[2] - run[-1][2] <= MAX_RULE_GAP:
                run.append(rule)
                continue
            if len(run) >= 3:
                regions.append((min(e[0] for e in run), run[0][2], max(e[1] for e in run), run[-1][2]))
            run = [rule] if rule is not None else []
    return regions


def _merge_intervals(intervals: list[tuple[float, float]], gap: float) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for x0, x1 in sorted(intervals):
        if merged and x0 - merged[-1][1] <= gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], x1))
        else:
            merged.append((x0, x1))
    return merged


def _text_lines(words: list[tuple]) -> list[tuple[float, float]]:
    """Vertical extents of the text lines formed by these words."""
    return _merge_intervals([(w[1] + 0.25 * (w[3] - w[1]), w[3] - 0.25 * (w[3] - w[1])) for w in words], 0.0)


def build_table(page: pymupdf.Page, region: BBox, edges: list[Edge]) -> tuple[list[list[str]], list[dict], int] | None:
    """Rows from horizontal edges, columns from text alignment. Returns (rows, spans, header_rows)."""
    x0, y0, x1, y1 = region
    width = x1 - x0
    ys = sorted(e[2] for e in edges
                if y0 - 1.5 <= e[2] <= y1 + 1.5 and e[0] >= x0 - 3 and e[1] <= x1 + 3
                and e[1] - e[0] >= 0.25 * width and (abs(e[0] - x0) <= 3 or abs(e[1] - x1) <= 3))
    levels: list[float] = []
    for y in ys:
        if not levels or y - levels[-1] > 1.5:
            levels.append(y)
    if len(levels) < 3:
        return None
    words = [w for w in page.get_text("words", clip=pymupdf.Rect(region)) if w[4].strip()]
    if len(words) < 4:
        return None

    def inside(band: tuple[float, float]) -> list[tuple]:
        return [w for w in words if band[0] <= (w[1] + w[3]) / 2 <= band[1]]

    bands = [(a, b) for a, b in zip(levels, levels[1:], strict=False) if b - a >= MIN_ROW_HEIGHT]
    bands = [b for b in bands if inside(b)]
    if len(bands) <= 3:                      # booktabs: rules only around the header and the body
        split: list[tuple[float, float]] = []
        for band in bands:
            lines = _text_lines(inside(band))
            if len(lines) >= 3:
                cuts = [band[0]] + [(a[1] + b[0]) / 2 for a, b in zip(lines, lines[1:], strict=False)] + [band[1]]
                split += list(zip(cuts, cuts[1:], strict=False))
            else:
                split.append(band)
        bands = split
    if len(bands) < 2 or sum(b - a for a, b in bands) < 0.6 * (y1 - y0):
        return None

    gap = max(6.0, 0.75 * statistics.median(w[3] - w[1] for w in words))
    segments = [_merge_intervals([(w[0], w[2]) for w in inside(band)], gap) for band in bands]
    ncols = max(len(s) for s in segments)
    if ncols < 2:
        return None
    reference = [s for s in segments if len(s) == ncols]
    columns = _merge_intervals([iv for s in reference for iv in s], 1.0)
    if len(columns) != ncols:
        columns = reference[0]
    bounds = [x0] + [(a[1] + b[0]) / 2 for a, b in zip(columns, columns[1:], strict=False)] + [x1]

    rows: list[list[str]] = []
    bold_rows: list[bool] = []
    for band, segs in zip(bands, segments, strict=True):
        row = [""] * ncols
        bold = []
        for seg in segs:
            seg_width = max(seg[1] - seg[0], 0.1)
            col = next((j for j in range(ncols)
                        if min(seg[1], bounds[j + 1]) - max(seg[0], bounds[j]) >= 0.25 * seg_width),
                       min(range(ncols), key=lambda j: abs((bounds[j] + bounds[j + 1]) / 2 - (seg[0] + seg[1]) / 2)))
            text, is_bold = cell_text(page, (seg[0] - 1, band[0], seg[1] + 1, band[1]))
            row[col] = f"{row[col]} {text}".strip()
            bold.append(is_bold)
        rows.append(row)
        bold_rows.append(bool(bold) and all(bold))

    header_rows = 0
    for is_bold in bold_rows[:-1]:
        if not is_bold:
            break
        header_rows += 1

    spans: list[dict] = []
    for r in range(header_rows):             # a header cell covers the empty cells to its right
        filled = [c for c in range(ncols) if rows[r][c]]
        if len(filled) < ncols:
            for c, nxt in zip(filled, filled[1:] + [ncols], strict=True):
                if nxt - 1 > c:
                    spans.append({"col_start": c, "row_start": r, "col_end": nxt - 1, "row_end": r})
    r = header_rows
    while r < len(rows):                     # a group label in column 0 covers the empty cells below it
        end = r
        while rows[r][0] and end + 1 < len(rows) and not rows[end + 1][0]:
            end += 1
        if end > r:
            spans.append({"col_start": 0, "row_start": r, "col_end": 0, "row_end": end})
        r = end + 1
    return rows, spans, header_rows


def _usable(rows: list[list[str]]) -> bool:
    if len(rows) < 2 or max((len(r) for r in rows), default=0) < 2:
        return False
    filled = sum(1 for r in rows for cell in r if cell)
    return filled >= 3 and sum(1 for r in rows if sum(bool(c) for c in r) >= 2) >= 2


def _grid_tables(page: pymupdf.Page) -> list[tuple[BBox, list[list[str]]]]:
    """Tables drawn as a full grid of cell borders."""
    try:
        tables = page.find_tables(strategy="lines_strict").tables
    except Exception:  # noqa: BLE001 - the table finder is best effort on odd PDFs
        return []
    out = []
    for t in tables:
        rows = [[cell_text(page, cell)[0] if cell else "" for cell in row.cells] for row in t.rows]
        rows = [r for r in rows if any(r)]
        out.append((tuple(t.bbox), rows))
    return out


def find_tables(page: pymupdf.Page, number: int, drawings: list[dict], clusters: list[BBox]) -> list[Chunk]:
    found: list[Chunk] = []
    page_area = abs(page.rect)

    def add(bbox: BBox, rows: list[list[str]], how: str, spans=(), header_rows: int = 0) -> None:
        if not _usable(rows) or _area(bbox) > MAX_PAGE_SHARE * page_area:
            return
        if any(_intersection(bbox, t.bbox) > 0.3 * min(_area(bbox), _area(t.bbox)) for t in found):
            return
        found.append(Chunk(id=f"p{number}t{len(found)}", page=number, bbox=bbox, text="", size=0.0, bold=False,
                           lines=len(rows), type="table", rows=rows, detection=how, spans=list(spans),
                           header_rows=header_rows))

    edges = _horizontal_edges(drawings)
    for region, how in [(c, "bands") for c in clusters] + [(r, "rules") for r in _rule_stacks(drawings)]:
        built = build_table(page, region, edges)
        if built:
            add(region, built[0], how, built[1], built[2])
    for bbox, rows in _grid_tables(page):
        add(bbox, rows, "grid")
    found.sort(key=lambda t: (t.bbox[1], t.bbox[0]))
    for i, t in enumerate(found):
        t.id = f"p{number}t{i}"
    return found


# ----------------------------------------------------------------------------- figures and boxes


def find_figures_and_boxes(page: pymupdf.Page, number: int, chunks: list[Chunk], image_boxes: list[BBox],
                           drawings: list[dict], clusters: list[BBox], tables: list[Chunk],
                           ) -> tuple[list[Chunk], list[BBox]]:
    page_area = abs(page.rect)
    boxes: list[BBox] = [b for b in image_boxes
                         if b[2] - b[0] >= MIN_FIGURE_SIDE and b[3] - b[1] >= MIN_FIGURE_SIDE]
    detection = {b: "image" for b in boxes}
    plain_boxes: list[BBox] = []

    for box in clusters:
        if box[2] - box[0] < MIN_FIGURE_SIDE or box[3] - box[1] < 20 or _area(box) > MAX_PAGE_SHARE * page_area:
            continue
        if any(_intersection(box, t.bbox) > 0.3 * _area(box) for t in tables):
            continue
        rect = pymupdf.Rect(box)
        parts = sum(1 for d in drawings if rect.contains(d["rect"]))
        if parts < FIGURE_MIN_PARTS:
            plain_boxes.append(box)          # a frame or shaded panel, not artwork
        elif box[3] - box[1] >= MIN_FIGURE_SIDE:
            boxes.append(box)
            detection[box] = "vector"

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
    figures = [Chunk(id=f"p{number}f{i}", page=number, bbox=box, text="", size=0.0, bold=False, lines=1,
                     type="figure", detection=how) for i, (box, how) in enumerate(merged)]
    plain_boxes = [b for b in plain_boxes if not any(_intersection(b, f.bbox) > 0.5 * _area(b) for f in figures)]
    return figures, plain_boxes


def _callout(number: int, index: int, box: BBox, inner: list[Chunk]) -> Chunk:
    inner = sorted(inner, key=lambda c: (round(c.bbox[1]), c.bbox[0]))
    items = [item for c in inner for item in (c.items or [c.text])]
    return Chunk(id=f"p{number}b{index}", page=number, bbox=box, text="\n".join(items),
                 size=statistics.fmean(c.size for c in inner), bold=False, lines=sum(c.lines for c in inner),
                 type="callout", items=items, font=inner[-1].font)


def detect_objects(page: pymupdf.Page, number: int, chunks: list[Chunk], image_boxes: list[BBox]) -> list[Chunk]:
    """Return the page's chunks with tables, figures and callouts added and their inner text removed."""
    try:
        drawings = page.get_drawings()
        clusters = [_box(r) for r in page.cluster_drawings(drawings=drawings)] if drawings else []
    except Exception:  # noqa: BLE001 - odd vector content must not stop text extraction
        drawings, clusters = [], []
    tables = find_tables(page, number, drawings, clusters) if drawings else []
    figures, boxes = find_figures_and_boxes(page, number, chunks, image_boxes, drawings, clusters, tables)
    if not (tables or figures or boxes):
        return chunks

    kept: list[Chunk] = []
    boxed: dict[BBox, list[Chunk]] = {}
    for c in chunks:
        if any(_center_in(c.bbox, t.bbox) for t in tables):
            continue                        # the text lives in table.rows
        owner = next((f for f in figures if _center_in(c.bbox, f.bbox, pad=0.0)), None)
        if owner is not None and not CAPTION_RE.match(c.text):
            owner.labels += c.items or [c.text]
            continue
        box = next((b for b in boxes if _center_in(c.bbox, b, pad=0.0)), None)
        if box is not None and not CAPTION_RE.match(c.text):
            boxed.setdefault(box, []).append(c)
        else:
            kept.append(c)
    callouts = []
    for box, inner in boxed.items():
        if sum(len(c.text) for c in inner) >= 20:
            callouts.append(_callout(number, len(callouts), box, inner))
        else:
            kept += inner                   # a decorative tile behind a numeral or an icon
    return kept + tables + figures + callouts


def render_region(page: pymupdf.Page, bbox: BBox, dpi: int = 200) -> tuple[bytes, int, int]:
    """PNG of a page region (figure or table), with everything drawn inside it."""
    clip = pymupdf.Rect(bbox) & page.rect
    pix = page.get_pixmap(clip=clip, dpi=dpi, alpha=False)
    return pix.tobytes("png"), pix.width, pix.height
