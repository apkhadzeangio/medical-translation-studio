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

import re
import statistics
from dataclasses import dataclass

import pymupdf

from app.ingest.pdf_layout import CAPTION_LABEL_RE, BBox, Chunk, _line_text, is_caption, join_lines

MIN_FIGURE_SIDE = 40.0      # points; smaller graphics are icons, bullets, rules
MAX_PAGE_SHARE = 0.85       # a graphic covering the whole page is a background or frame
RULE_THICKNESS = 2.5
MIN_RULE_WIDTH = 80.0
MIN_ROW_HEIGHT = 6.0
MAX_RULE_GAP = 200.0        # rules further apart than this do not belong to one table
FIGURE_MIN_PARTS = 4        # fewer drawing parts than this is a box, not artwork
MIN_ART_SIDE = 12.0         # a curved path at least this large is artwork, not a table border
MIN_PANEL_SIDE = 60.0
UNFINISHED_RE = re.compile(r"(?:[,/–-]|\b(?:for|of|the|and|or|to|in|with|an?))$")
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


def _overlap_x(a: BBox, b: BBox) -> float:
    return min(a[2], b[2]) - max(a[0], b[0])


def _inside(inner: BBox, outer: BBox, pad: float = 2.0) -> bool:
    return (inner[0] >= outer[0] - pad and inner[1] >= outer[1] - pad
            and inner[2] <= outer[2] + pad and inner[3] <= outer[3] + pad)


def _art_boxes(drawings: list[dict]) -> list[BBox]:
    """Curved paths: outlines of bodies, vessels, waveforms. Tables are drawn with straight lines only."""
    return [_box(d["rect"]) for d in drawings
            if d["rect"].width >= MIN_ART_SIDE and d["rect"].height >= MIN_ART_SIDE
            and any(item[0] == "c" for item in d["items"])]


def _fill_rects(drawings: list[dict]) -> list[BBox]:
    """Filled rectangles: caption bars, table header and body bands, highlighted labels."""
    return [_box(d["rect"]) for d in drawings
            if d.get("fill") is not None and d["type"] in ("f", "fs") and len(d["items"]) <= 5
            and all(item[0] in ("re", "l", "qu") for item in d["items"])
            and d["rect"].width >= 20 and d["rect"].height >= MIN_ROW_HEIGHT]


def _has_art(region: BBox, art: list[BBox], images: list[BBox]) -> bool:
    """Does the region hold a picture: a raster image or curved line art."""
    return (any(_intersection(region, b) > 0.05 * min(_area(region), _area(b)) for b in images if _area(b) > 0)
            or any(_center_in(b, region, pad=0.0) for b in art))


def _components(rects: list[BBox], gap: float = 1.5) -> list[BBox]:
    """Bounding boxes of the groups of rectangles that touch or overlap."""
    groups: list[BBox] = []
    for rect in sorted(rects, key=lambda r: (r[1], r[0])):
        merged = rect
        rest = []
        for g in groups:
            grown = (g[0] - gap, g[1] - gap, g[2] + gap, g[3] + gap)
            if _intersection(merged, grown) > 0 or _inside(merged, grown, 0):
                merged = (min(g[0], merged[0]), min(g[1], merged[1]), max(g[2], merged[2]), max(g[3], merged[3]))
            else:
                rest.append(g)
        groups = rest + [merged]
    return groups


@dataclass
class Section:
    """The part of a framed panel that one caption describes."""

    caption: Chunk
    bbox: BBox
    panel: BBox

    @property
    def is_table(self) -> bool:
        return self.caption.text.lower().startswith("table")


def panel_sections(panels: list[BBox], captions: list[Chunk], fills: list[BBox], art: list[BBox],
                   images: list[BBox]) -> list[Section]:
    """Split each framed panel into the regions its captions head.

    Textbooks of the "at a glance" kind print one frame per page that holds several captioned
    figures and tables. A caption sits in a shaded bar; what it describes lies below the bar,
    down to the next bar or the bottom of the frame.
    """
    sections: list[Section] = []
    for panel in panels:
        inside = [c for c in captions if _center_in(c.bbox, panel, pad=0.0)]
        bars: dict[str, BBox | None] = {}
        for cap in inside:
            holders = [f for f in fills if _center_in(cap.bbox, f, pad=0.0) and f[3] - f[1] <= 80
                       and _inside(f, panel, 3.0)]
            bars[cap.id] = min(holders, key=_area) if holders else None
        for cap in inside:
            bar = bars[cap.id]
            head = bar or cap.bbox
            x0, x1 = (bar[0], bar[2]) if bar else (panel[0], panel[2])
            others = [bars[o.id] or o.bbox for o in inside if o is not cap]
            below = [o[1] for o in others if o[1] >= head[3] - 1 and _overlap_x(o, (x0, 0, x1, 0)) > 0]
            region = (x0, head[3], x1, min(below, default=panel[3]))
            if bar is None and not _has_art(region, art, images):
                # a plain caption printed under its picture, inside the frame
                above = [o[3] for o in others if o[3] <= head[1] + 1 and _overlap_x(o, (x0, 0, x1, 0)) > 0]
                region = (x0, max(above, default=panel[1]), x1, head[1])
            if region[3] - region[1] >= 10:
                sections.append(Section(cap, region, panel))
    return sections


# ----------------------------------------------------------------------------- formulas


def _join_tokens(words: list[tuple]) -> str:
    """Words of one printed line in left-to-right order; glyphs that touch are one token."""
    text, right = "", None
    for w in sorted(words, key=lambda w: w[0]):
        text += ("" if right is None or w[0] - right <= 1.0 else " ") + w[4]
        right = w[2]
    return text.strip()


def _rules(drawings: list[dict]) -> list[Edge]:
    """Every thin horizontal line on the page, also those drawn as one segment of a longer path."""
    rules: list[Edge] = []
    for d in drawings:
        for item in d["items"]:
            if item[0] == "l" and abs(item[1].y - item[2].y) < 0.7:
                rules.append((min(item[1].x, item[2].x), max(item[1].x, item[2].x), (item[1].y + item[2].y) / 2))
            elif item[0] == "re" and item[1].height <= 1.5:
                rules.append((item[1].x0, item[1].x1, (item[1].y0 + item[1].y1) / 2))
    return rules


def find_fractions(page: pymupdf.Page, number: int, drawings: list[dict], tables: list[Chunk]) -> list[Chunk]:
    """Formulas printed as a fraction: a short rule with one centred line above and one below.

    The text layer of such a formula is a jumble of lines (numerator, denominator, brackets and
    "= X" each on their own), so it is taken out of the flow as one block. The block keeps the
    parts in a fixed order for reference; the rendered image is what the reader gets.
    """
    rules = [r for r in _rules(drawings) if 15 <= r[1] - r[0] <= 330]
    if not rules:
        return []
    words = [w for w in page.get_text("words") if w[4].strip()]
    if not words:
        return []
    h = statistics.median(w[3] - w[1] for w in words)
    found: list[Chunk] = []
    for x0, x1, y in rules:
        length = x1 - x0
        if any(t.bbox[0] - 2 <= x0 and x1 <= t.bbox[2] + 2 and t.bbox[1] - 2 <= y <= t.bbox[3] + 2 for t in tables):
            continue
        if any(o is not r and abs(o[2] - y) <= 45 and abs(o[2] - y) > 0.5
               and min(o[1], x1) - max(o[0], x0) >= 0.7 * max(length, o[1] - o[0])
               for r in [(x0, x1, y)] for o in rules):
            continue                                  # one of several stacked rules: a table, not a fraction

        def straddles(w, y=y) -> bool:
            return w[1] < y - 0.25 * h and w[3] > y + 0.25 * h

        def within(w, x0=x0, x1=x1) -> bool:
            return x0 - 2 <= (w[0] + w[2]) / 2 <= x1 + 2

        above = [w for w in words if within(w) and not straddles(w) and y - 1.5 * h <= w[3] <= y + 1.5]
        below = [w for w in words if within(w) and not straddles(w) and y - 1.5 <= w[1] <= y + 1.5 * h]
        if not above or not below:
            continue
        above = [w for w in above if w[3] >= max(v[3] for v in above) - 0.5 * h]      # the nearest line only
        below = [w for w in below if w[1] <= min(v[1] for v in below) + 0.5 * h]
        if y - max(w[3] for w in above) > 0.9 * h or min(w[1] for w in below) - y > 0.9 * h:
            continue

        def centred(line: list[tuple], x0=x0, x1=x1, length=length) -> bool:
            a, b = min(w[0] for w in line), max(w[2] for w in line)
            return abs((a + b) / 2 - (x0 + x1) / 2) <= 0.25 * length + 3 and b - a <= length + 6

        def continues(line: list[tuple], x0=x0, x1=x1, within=within, straddles=straddles) -> bool:
            """Does the printed line run on beyond the rule (then the rule is an underline)."""
            top, bottom = min(w[1] for w in line), max(w[3] for w in line)
            return any(not within(w) and not straddles(w) and w[1] < bottom - 0.5 * h and w[3] > top + 0.5 * h
                       and (0 <= x0 - w[2] <= 10 or 0 <= w[0] - x1 <= 10) for w in words)

        if not (centred(above) and centred(below)) or continues(above) or continues(below):
            continue
        left, edge = [], x0
        for w in sorted((w for w in words if straddles(w) and w[2] <= x0 + 2), key=lambda w: -w[2]):
            if edge - w[2] > 16:
                break
            left.append(w)
            edge = w[0]
        right, edge = [], x1
        for w in sorted((w for w in words if straddles(w) and w[0] >= x1 - 2), key=lambda w: w[0]):
            if w[0] - edge > 16:
                break
            right.append(w)
            edge = w[2]
        if not left and not right:
            continue                                  # no "= X": two centred lines around a rule are not a formula
        parts = {"left": _join_tokens(left), "numerator": _join_tokens(above),
                 "denominator": _join_tokens(below), "right": _join_tokens(right)}
        every = left + above + below + right
        bbox = (min(min(w[0] for w in every), x0) - 1, min(w[1] for w in every) - 1,
                max(max(w[2] for w in every), x1) + 1, max(w[3] for w in every) + 1)
        text = " ".join(t for t in (parts["left"], f"({parts['numerator']}) / ({parts['denominator']})",
                                    parts["right"]) if t)
        found.append(Chunk(id=f"p{number}e{len(found)}", page=number, bbox=bbox, text=text, size=h, bold=False,
                           lines=1, type="equation", detection="fraction", extra=parts))
    return found


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


def find_tables(page: pymupdf.Page, number: int, drawings: list[dict], clusters: list[BBox],
                art: list[BBox] = (), images: list[BBox] = (), sections: list[Section] = (),
                fills: list[BBox] = ()) -> list[Chunk]:
    found: list[Chunk] = []
    page_area = abs(page.rect)

    def add(bbox: BBox, rows: list[list[str]], how: str, spans=(), header_rows: int = 0) -> None:
        if not _usable(rows) or _area(bbox) > MAX_PAGE_SHARE * page_area:
            return
        if _has_art(bbox, list(art), list(images)):
            return                          # labels around a drawing line up by accident; that is not a table
        if any(_intersection(bbox, t.bbox) > 0.3 * min(_area(bbox), _area(t.bbox)) for t in found):
            return
        found.append(Chunk(id=f"p{number}t{len(found)}", page=number, bbox=bbox, text="", size=0.0, bold=False,
                           lines=len(rows), type="table", rows=rows, detection=how, spans=list(spans),
                           header_rows=header_rows))

    edges = _horizontal_edges(drawings)
    panels = [s.panel for s in sections]
    # inside a framed panel a table is a group of shaded bands; the frame itself is not a table
    bars = [f for f in fills if any(_center_in(s.caption.bbox, f, pad=0.0) for s in sections)]
    banded = [g for s in sections
              for g in _components([f for f in fills if f not in bars and _inside(f, s.bbox, 3.0)
                                    and _area(f) < 0.8 * _area(s.bbox)])]
    regions = [(c, "bands") for c in clusters if c not in panels] + [(g, "bands") for g in banded] \
        + [(r, "rules") for r in _rule_stacks(drawings) if not any(_center_in(r, p, pad=0.0) for p in panels)]
    for region, how in regions:
        built = build_table(page, region, edges)
        if built:
            add(region, built[0], how, built[1], built[2])
    for bbox, rows in _grid_tables(page):
        if not any(_intersection(bbox, p) > 0.5 * _area(bbox) for p in panels):
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


def _join_labels(texts: list[Chunk]) -> list[Chunk]:
    """Lines of one figure label that the PDF stores as separate blocks become one label again.

    Two pieces belong together when the second sits directly under the first at normal line
    spacing and shares its left edge, right edge or centre.
    """
    groups: list[Chunk] = []
    for c in sorted(texts, key=lambda c: (c.bbox[1], c.bbox[0])):
        for g in groups:
            aligned = min(abs(c.bbox[0] - g.bbox[0]), abs(c.bbox[2] - g.bbox[2]),
                          abs((c.bbox[0] + c.bbox[2]) - (g.bbox[0] + g.bbox[2])) / 2) <= 3.0
            # a new label starts with a capital; a wrapped line does not, unless the line before is unfinished
            carries_on = bool(re.match(r"[a-z(\d]", c.text) or UNFINISHED_RE.search(g.text))
            if (aligned and carries_on and not g.items and not c.items and abs(c.size - g.size) < 0.5
                    and -0.5 * c.size <= c.bbox[1] - g.bbox[3] <= 0.35 * c.size):
                g.text = join_lines([g.text, c.text])
                g.bbox = _union([g.bbox, c.bbox])
                g.lines += c.lines
                break
        else:
            groups.append(c)
    return groups


def _section_figure(section: Section, number: int, index: int, drawings: list[dict], images: list[BBox],
                    taken: list[BBox], texts: list[Chunk], art: list[BBox]) -> Chunk | None:
    """The picture a caption describes: everything drawn in its section that is not a table or formula."""
    def free(box: BBox) -> bool:
        return not any(_center_in(box, t, pad=0.0) for t in taken)

    content = [b for b in images if _center_in(b, section.bbox, pad=0.0) and free(b)]
    how = "image" if content else "vector"
    shapes = [_box(d["rect"]) for d in drawings
              if _inside(_box(d["rect"]), section.bbox, 2.0) and free(_box(d["rect"]))
              and max(d["rect"].width, d["rect"].height) >= 4]
    if content and len(shapes) >= FIGURE_MIN_PARTS:
        how = "mixed"
    if not content and len(shapes) < FIGURE_MIN_PARTS and not any(_center_in(a, section.bbox, pad=0.0) for a in art):
        return None
    texts = _join_labels(texts)
    boxes = content + shapes + [c.bbox for c in texts]
    x0, y0, x1, y1 = _union(boxes)
    s = section.bbox
    bbox = (max(x0, s[0]), max(y0, s[1]), min(x1, s[2]), min(y1, s[3]))
    figure = Chunk(id=f"p{number}f{index}", page=number, bbox=bbox, text="", size=0.0, bold=False, lines=1,
                   type="figure", detection=how, caption_id=section.caption.id)
    for c in sorted(texts, key=lambda c: (round(c.bbox[1] / 6), c.bbox[0])):
        for item in c.items or [c.text]:
            figure.labels.append(item)
            figure.label_boxes.append(c.bbox)
    return figure


def _union(boxes: list[BBox]) -> BBox:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def detect_objects(page: pymupdf.Page, number: int, chunks: list[Chunk], image_boxes: list[BBox]) -> list[Chunk]:
    """Return the page's chunks with tables, figures, formulas and callouts added and their inner text removed."""
    try:
        drawings = page.get_drawings()
        clusters = [_box(r) for r in page.cluster_drawings(drawings=drawings)] if drawings else []
    except Exception:  # noqa: BLE001 - odd vector content must not stop text extraction
        drawings, clusters = [], []
    if not drawings and not image_boxes:
        return chunks
    page_area = abs(page.rect)
    art, fills = _art_boxes(drawings), _fill_rects(drawings)
    for c in chunks:                        # "Table 17.1 percentage (%) ..." in a shaded bar is a caption
        if CAPTION_LABEL_RE.match(c.text) and any(
                _center_in(c.bbox, f, pad=0.0) and f[3] - f[1] <= 80 for f in fills):
            c.in_caption_bar = True
    captions = [c for c in chunks if is_caption(c)]
    panels = [box for box in clusters
              if box[2] - box[0] >= MIN_PANEL_SIDE and box[3] - box[1] >= MIN_PANEL_SIDE
              and _area(box) <= MAX_PAGE_SHARE * page_area
              and any(_center_in(c.bbox, box, pad=0.0) for c in captions)]
    sections = panel_sections(panels, captions, fills, art, image_boxes)
    panels = [p for p in panels if any(s.panel == p for s in sections)]

    tables = find_tables(page, number, drawings, clusters, art, image_boxes, sections, fills) if drawings else []
    formulas = find_fractions(page, number, drawings, tables) if drawings else []
    taken = [t.bbox for t in tables] + [f.bbox for f in formulas]

    def in_panel(box: BBox) -> bool:
        return any(_center_in(box, p, pad=0.0) for p in panels)

    figures, boxes = find_figures_and_boxes(
        page, number, chunks, [b for b in image_boxes if not in_panel(b)], drawings,
        [c for c in clusters if c not in panels], tables)
    section_text: dict[int, list[Chunk]] = {}
    for c in chunks:
        if is_caption(c) or any(_center_in(c.bbox, t, pad=1.0) for t in taken):
            continue
        home = next((i for i, s in enumerate(sections) if _center_in(c.bbox, s.bbox, pad=0.0)), None)
        if home is not None and not sections[home].is_table:
            section_text.setdefault(home, []).append(c)
    section_figures: dict[str, int] = {}
    for i, section in enumerate(sections):
        if section.is_table:
            for t in tables:                # the table under a "Table n" bar is that caption's table
                if t.caption_id is None and _center_in(t.bbox, section.bbox, pad=0.0):
                    t.caption_id = section.caption.id
                    break
            continue
        figure = _section_figure(section, number, len(figures), drawings, image_boxes, taken,
                                 section_text.get(i, []), art)
        if figure is not None:
            figures.append(figure)
            section_figures[figure.id] = i
    if not (tables or figures or boxes or formulas):
        return chunks

    kept: list[Chunk] = []
    boxed: dict[BBox, list[Chunk]] = {}
    for c in chunks:
        if any(_center_in(c.bbox, t.bbox) for t in tables):
            continue                        # the text lives in table.rows
        if any(_center_in(c.bbox, f.bbox, pad=1.0) for f in formulas):
            continue                        # the text lives in the formula block
        if is_caption(c):
            kept.append(c)
            continue
        home = next((i for i, s in enumerate(sections) if _center_in(c.bbox, s.bbox, pad=0.0)), None)
        if home is not None and home in section_figures.values():
            continue                        # already a label of that section's figure
        owner = next((f for f in figures if f.id not in section_figures
                      and _center_in(c.bbox, f.bbox, pad=0.0)), None)
        if owner is not None:
            for item in c.items or [c.text]:
                owner.labels.append(item)
                owner.label_boxes.append(c.bbox)
            continue
        box = next((b for b in boxes if _center_in(c.bbox, b, pad=0.0)), None)
        if box is not None:
            boxed.setdefault(box, []).append(c)
        else:
            kept.append(c)
    callouts = []
    for box, inner in boxed.items():
        if sum(len(c.text) for c in inner) >= 20:
            callouts.append(_callout(number, len(callouts), box, inner))
        else:
            kept += inner                   # a decorative tile behind a numeral or an icon
    return kept + tables + figures + formulas + callouts


def render_region(page: pymupdf.Page, bbox: BBox, dpi: int = 200) -> tuple[bytes, int, int]:
    """PNG of a page region (figure or table), with everything drawn inside it."""
    clip = pymupdf.Rect(bbox) & page.rect
    pix = page.get_pixmap(clip=clip, dpi=dpi, alpha=False)
    return pix.tobytes("png"), pix.width, pix.height
