"""Deterministic PDF layout analysis: text layer -> typed chunks in reading order.

The text always comes from the PDF text layer, character for character. Nothing here
(or in the optional model step in pdf_structure.py) rewrites source text.

Pipeline per document:
  1. extract chunks (text, bbox, font size, bold) from every page
  2. drop running headers/footers and page numbers
  3. order chunks with a recursive column cut (handles 1..n columns and full-width blocks)
  4. classify: heading / paragraph / caption / footnote / reference / list_item
  5. merge paragraphs that continue across a column or page break
  6. split into chapters (PDF outline first, then heading patterns)

Tables and figures are detected per page (pdf_objects.py), take their place in the reading
order as single chunks, and swallow the text that lies inside them.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

BBox = tuple[float, float, float, float]

LIGATURES = str.maketrans({"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "­": ""})
# "Table 17.1 Title" / "FIGURE 3 Title" are captions; "Table 17.1 is continued ..." is a sentence.
CAPTION_RE = re.compile(r"^(?i:Figure|Fig\.?|Table|Box|Algorithm)\s+\d+[\w.\-]*(?=$|[.:]?\s+[A-Z0-9(]|\s*[—–:|])")
# the label alone; with what follows it is a caption only where the page geometry says so (a caption bar)
CAPTION_LABEL_RE = re.compile(r"^(?i:Figure|Fig\.?|Table|Box|Algorithm)\s+\d+(?:[.\-]\d+)*[a-z]?\b")
LIST_RE = re.compile(r"^(?:\d{1,2}[.)]|[a-z][.)]|[•·▪■●◦‣–-])\s+\S")
NOTE_RE = re.compile(r"^(?:[*†‡§¶]|Note[s:.]|Abbreviations?\b|Source[s:.])")
REFERENCE_RE = re.compile(r"^\[?\d{1,3}[\].]\s+[A-Z][\w'’\-]+,?\s+[A-Z]{1,3}\b")
REFERENCES_HEADING_RE = re.compile(r"^(selected |key )?(references|bibliography)\b", re.I)
CHAPTER_RE = re.compile(r"^(chapter|section|part)\s+(\d+|[ivxlc]+)\b", re.I)
BULLET_RE = re.compile(r"^[•·▪■●◦‣–-]\s+")
DOT_BULLET_RE = re.compile(r"^[•·▪■●◦‣]\s+\S")          # unambiguous bullets (a dash can open a wrapped line)
STEP_RE = re.compile(r"^(\d{1,2})\s+[A-Z(]")          # "7 Inflate cuff ...": a numbered step without a period
FOLIO_RE = re.compile(r"^(\d{1,4})\b.*\D|\D.*\b(\d{1,4})$", re.S)
PAGE_NUMBER_RE = re.compile(r"^(page\s+)?(\d{1,4}|[ivxlcdm]{1,7})$", re.I)
SENTENCE_END = (".", "?", "!", ":", ";", "”", '"', ")", "]")

MARGIN_BAND = 0.09      # top/bottom share of the page where running heads live
MIN_GUTTER = 9.0        # points of clear horizontal space that count as a column gap
OBJECT_TYPES = ("table", "figure")
FIXED_TYPES = ("table", "figure", "callout")   # set by detection, never reclassified
SIDE_BY_SIDE_GAP = 12.0  # points between two text runs on one row that makes them separate chunks
HEADING_DELTA = 1.2     # points above body size that make a heading
SPLIT_DELTA = 1.2       # font-size change that splits one PDF block into two chunks


@dataclass
class Chunk:
    id: str
    page: int                       # 1-based
    bbox: BBox
    text: str
    size: float
    bold: bool
    lines: int
    type: str = "paragraph"
    level: int | None = None
    parts: list[tuple[int, BBox]] = field(default_factory=list)  # continuation fragments
    type_locked: bool = False       # type set by the structure model; heuristics must not override
    in_caption_bar: bool = False    # printed in a shaded caption bar: a caption whatever its wording
    rows: list[list[str]] | None = None     # table cells, exact text
    labels: list[str] = field(default_factory=list)   # text found inside a figure
    caption_id: str | None = None   # caption chunk that belongs to this table/figure
    detection: str | None = None    # how a table/figure was found
    font: str = ""
    items: list[str] = field(default_factory=list)    # lines of a list or callout, one segment each
    spans: list[dict] = field(default_factory=list)   # merged table cells
    header_rows: int = 0
    label: str | None = None        # "17.1" from the caption
    continues: str | None = None    # id of the first fragment when a table continues on a later page
    repeated_header_rows: int = 0   # leading rows that only repeat the first fragment's header
    note_of: str | None = None      # table this note belongs to
    label_boxes: list[BBox] = field(default_factory=list)   # where each figure label is printed
    dropped: bool = False           # the model called it page furniture
    extra: dict = field(default_factory=dict)         # detector-specific details (formula parts, ...)

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]


@dataclass
class PageInfo:
    number: int
    width: float
    height: float
    columns: int = 1
    image_area: float = 0.0         # share of the page covered by images
    scanned: bool = False
    complex: bool = False
    tables: int = 0
    figures: int = 0
    chunk_ids: list[str] = field(default_factory=list)
    read_by_model: bool = False     # the model step decided this page's structure
    model_notes: list[str] = field(default_factory=list)   # parts of its answer that failed a check


@dataclass
class ChapterSpan:
    title: str
    page_start: int
    page_end: int
    first_chunk: int                # index into ParsedDocument.chunks


@dataclass
class ParsedDocument:
    pages: list[PageInfo]
    chunks: list[Chunk]             # reading order, headers/footers removed
    chapters: list[ChapterSpan]
    body_size: float
    dropped: int = 0                # running headers, footers, page numbers
    warnings: list[str] = field(default_factory=list)
    # structure the parser is not sure about: {"page", "code", "severity": "error"|"warning", "message"}.
    # An "error" means the stored structure is known to be damaged; translation is held back.
    issues: list[dict] = field(default_factory=list)

    def report(self, page: int, code: str, severity: str, message: str) -> None:
        self.issues.append({"page": page, "code": code, "severity": severity, "message": message})
        self.warnings.append(f"p{page}: {message}")


# ----------------------------------------------------------------------------- extraction


def _line_text(line: dict) -> tuple[str, float, bool, str]:
    """One PDF line -> (text with <sup>/<sub> markup, body font size, is bold, dominant font)."""
    spans = [sp for sp in line["spans"] if sp["text"]]
    main = max(spans, key=lambda sp: (sp["size"], len(sp["text"])), default=None)
    out: list[str] = []
    sizes: list[tuple[float, int]] = []
    fonts: Counter[str] = Counter()
    bold_chars = total = 0
    for span in spans:
        text = span["text"].translate(LIGATURES)
        stripped = text.strip()
        small = stripped and len(stripped) <= 12 and span is not main and span["size"] <= 0.87 * main["size"]
        shift = span.get("origin", (0, 0))[1] - main.get("origin", (0, 0))[1]   # > 0 means lower on the page
        if stripped and len(stripped) <= 12 and (span["flags"] & 1 or (small and shift <= -0.2 * main["size"])):
            # superscript: citation markers and exponents must not fuse with the word before
            out.append(f"<sup>{stripped}</sup>")
            continue
        if small and shift >= 0.1 * main["size"]:
            out.append(f"<sub>{stripped}</sub>" + (" " if text.endswith(" ") else ""))
            continue
        out.append(text)
        n = len(stripped)
        sizes.append((span["size"], n))
        fonts[span["font"]] += n
        total += n
        if span["flags"] & 16 or "bold" in span["font"].lower():
            bold_chars += n
    text = re.sub(r"[ \t\u00a0]+", " ", "".join(out)).strip()
    weight = sum(n for _, n in sizes)
    size = sum(sz * n for sz, n in sizes) / weight if weight else 0.0
    font = fonts.most_common(1)[0][0] if fonts else ""
    return text, size, bool(total) and bold_chars / total > 0.6, font


def join_lines(lines: list[str]) -> str:
    """Join wrapped lines; a trailing hyphen before a lowercase continuation is a line-break hyphen."""
    text = ""
    for line in lines:
        if not text:
            text = line
        elif re.search(r"[A-Za-z]-$", text) and line[:1].islower():
            text = text[:-1] + line
        else:
            text = f"{text} {line}"
    return text


def _list_run(group: list[tuple]) -> tuple[int, int, list[int]]:
    """Where a list sits inside one PDF text block: (first line, end line, marker lines).

    A block that opens with marker lines is a list. A block may also hold a sentence, then
    bullets or numbered steps, then the next paragraph; those are found only from unambiguous
    signs (dot bullets, or step numbers that count up one by one).
    """
    texts = [g[0] for g in group]
    markers = [i for i, t in enumerate(texts) if LIST_RE.match(t)]
    if not (len(markers) >= 2 and markers[0] == 0):
        markers = [i for i, t in enumerate(texts) if DOT_BULLET_RE.match(t)]
        if len(markers) == 1 and markers[0] > 0:
            markers = markers * 2                     # one bullet after its lead-in; the list goes on elsewhere
        if len(markers) < 2:
            steps = [(i, int(m.group(1))) for i, t in enumerate(texts) if (m := STEP_RE.match(t))]
            best: list[tuple[int, int]] = []
            run: list[tuple[int, int]] = []
            for step in steps:
                run = run + [step] if run and step[1] == run[-1][1] + 1 else [step]
                if len(run) > len(best):
                    best = run
            markers = [i for i, _ in best] if len(best) >= 3 else []
    if len(markers) < 2:
        return 0, len(group), []
    markers = sorted(set(markers))
    if markers[0] == 0 and LIST_RE.match(texts[0]) and not DOT_BULLET_RE.match(texts[0]) \
            and not STEP_RE.match(texts[0]):
        return 0, len(group), markers                     # classic numbered/lettered list: the whole block
    # The list ends where a line that is not a marker follows a finished item: an item line that
    # stops short of the right edge and closes its sentence is not wrapped onto the next line.
    left, right = min(g[3][0] for g in group), max(g[3][2] for g in group)
    end = markers[0] + 1
    while end < len(group):
        prev = group[end - 1]
        closed = prev[0].rstrip().endswith((".", ":", ";", "?", "!"))
        short = prev[3][2] < right - 0.12 * (right - left)
        run_in = closed and group[end][5] and not group[markers[0]][5]      # a bold run-in label starts here
        if end not in markers and (short and closed or run_in or end > markers[-1] and short):
            break
        end += 1
    return markers[0], end, markers


def _union(boxes: list[BBox]) -> BBox:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def is_caption(c: Chunk) -> bool:
    return bool(CAPTION_RE.match(c.text)) or (c.in_caption_bar and bool(CAPTION_LABEL_RE.match(c.text)))


def join_caption_labels(chunks: list[Chunk]) -> list[Chunk]:
    """"Figure 13.1" set apart from its sentence on the same line is one caption."""
    out = list(chunks)
    for label in [c for c in chunks if CAPTION_LABEL_RE.fullmatch(c.text.strip().rstrip(".:"))]:
        rest = [c for c in out if c is not label and -3 <= c.bbox[0] - label.bbox[2] <= 40
                and min(c.bbox[3], label.bbox[3]) - max(c.bbox[1], label.bbox[1])
                > 0.5 * (label.bbox[3] - label.bbox[1]) and c.bbox[1] >= label.bbox[1] - 3]
        if not rest:
            continue
        text = min(rest, key=lambda c: c.bbox[0])
        label.text = f"{label.text} {text.text}"
        label.bbox = _union([label.bbox, text.bbox])
        label.lines = max(label.lines, text.lines)
        label.bold = False
        out.remove(text)
    return out


def extract_page(page: pymupdf.Page, number: int) -> tuple[PageInfo, list[Chunk]]:
    data = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_LIGATURES)
    info = PageInfo(number=number, width=page.rect.width, height=page.rect.height)
    chunks: list[Chunk] = []
    image_area = 0.0
    for block in data["blocks"]:
        if block["type"] == 1:
            x0, y0, x1, y1 = block["bbox"]
            image_area += max(0.0, x1 - x0) * max(0.0, y1 - y0)
            continue
        group: list[tuple[str, float, bool, BBox, str, bool]] = []

        def emit(lines, markers=None):
            if not lines:
                return
            chunk = Chunk(
                id=f"p{number}c{len(chunks)}", page=number, bbox=_union([g[3] for g in lines]),
                text=join_lines([g[0] for g in lines]), size=statistics.fmean(g[1] for g in lines),
                bold=all(g[2] for g in lines), lines=len(lines),
                font=Counter(g[4] for g in lines).most_common(1)[0][0],
            )
            if markers:
                # several marker lines in one block: a list; wrapped lines stay with their item
                bounds = markers + [len(lines)]
                chunk.items = [join_lines([g[0] for g in lines[a:b]]) for a, b in zip(bounds, bounds[1:], strict=False)]
                chunk.text = "\n".join(chunk.items)
                chunk.type = "list"
            if chunk.text:
                chunks.append(chunk)

        def flush(group=group):
            if not group:
                return
            rest = list(group)
            while rest:
                first, end, markers = _list_run(rest)
                if not markers:
                    emit(rest)
                    break
                emit(rest[:first])                        # the sentence that introduces the list
                emit(rest[first:end], [m - first for m in markers if m < end])
                rest = rest[end:]                         # what follows: a paragraph, a sub-heading, more bullets
            group.clear()

        for line in block.get("lines", []):
            if abs(line["dir"][0] - 1) > 0.01:   # rotated text (margin labels, watermarks)
                continue
            text, size, bold, font = _line_text(line)
            if not text or not size:
                continue
            box = tuple(line["bbox"])
            if group:
                prev = group[-1]
                size_jump = abs(size - prev[1]) >= SPLIT_DELTA
                bold_end = prev[2] and not bold and len(group) <= 2 and all(g[2] for g in group)
                shared = min(box[3], prev[3][3]) - max(box[1], prev[3][1])
                same_row = shared > 0.3 * min(box[3] - box[1], prev[3][3] - prev[3][1])
                apart = same_row and (box[0] - prev[3][2] > SIDE_BY_SIDE_GAP or prev[3][0] - box[2] > SIDE_BY_SIDE_GAP)
                # a bold heading directly above a bold run-in label set in another typeface
                new_face = (bold and len(group) <= 2 and all(g[2] for g in group)
                            and font.split("-")[0] != prev[4].split("-")[0])
                if size_jump or bold_end or apart or new_face:
                    flush()
            lead = next((sp for sp in line["spans"] if sp["text"].strip()), None)
            lead_bold = bool(lead and (lead["flags"] & 16 or "bold" in lead["font"].lower()))
            group.append((text, size, bold, box, font, lead_bold))
        flush()
    info.image_area = min(1.0, image_area / (info.width * info.height))
    info.scanned = sum(len(c.text) for c in chunks) < 20 and info.image_area > 0.5
    chunks = join_caption_labels(chunks)
    if not info.scanned:
        from app.ingest.pdf_objects import detect_objects  # local import: pdf_objects uses Chunk
        image_boxes = [tuple(b["bbox"]) for b in data["blocks"] if b["type"] == 1]
        chunks = detect_objects(page, number, chunks, image_boxes)
        info.tables = sum(c.type == "table" for c in chunks)
        info.figures = sum(c.type == "figure" for c in chunks)
    return info, chunks


# ----------------------------------------------------------------------------- headers / footers


def _running_key(text: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"<sup>.*?</sup>", "", text)).strip().lower()


def drop_running_heads(pages: list[PageInfo], per_page: list[list[Chunk]]) -> int:
    """Remove page numbers and text repeated in the top/bottom margin across pages."""
    def in_margin(c: Chunk, p: PageInfo) -> bool:
        return c.bbox[3] <= p.height * MARGIN_BAND or c.bbox[1] >= p.height * (1 - MARGIN_BAND)

    def folio(c: Chunk) -> int | None:
        """The page number a marginal line carries at its start or end ("Chapter title  35")."""
        text = re.sub(r"<sup>.*?</sup>", "", c.text).strip()
        if c.lines > 2 or len(text) > 200:
            return None
        if PAGE_NUMBER_RE.match(text) and text.isdigit():
            return int(text)
        m = FOLIO_RE.match(text)
        return int(m.group(1) or m.group(2)) if m else None

    seen: Counter[str] = Counter()
    offsets: Counter[int] = Counter()                 # printed page number minus position in the file
    for page, chunks in zip(pages, per_page, strict=True):
        marginal = [c for c in chunks if in_margin(c, page)]
        for key in {_running_key(c.text) for c in marginal if len(c.text) < 400}:
            seen[key] += 1
        for offset in {n - page.number for c in marginal if (n := folio(c)) is not None}:
            offsets[offset] += 1
    threshold = max(2, min(3, len(pages))) if len(pages) < 6 else max(3, len(pages) // 10)
    # a running head that changes with every chapter is still furniture when it carries the folio
    numbering = {o for o, n in offsets.items() if n >= max(3, len(pages) // 4)} if len(pages) >= 4 else set()
    dropped = 0
    for page, chunks in zip(pages, per_page, strict=True):
        keep = []
        for c in chunks:
            marginal = in_margin(c, page)
            if marginal and (PAGE_NUMBER_RE.match(c.text.strip())
                             or (len(pages) > 1 and seen[_running_key(c.text)] >= threshold)
                             or (folio(c) is not None and folio(c) - page.number in numbering)):
                dropped += 1
            else:
                keep.append(c)
        chunks[:] = keep
    return dropped


# ----------------------------------------------------------------------------- reading order


def _gutter(chunks: list[Chunk]) -> float | None:
    """x position of the widest clear vertical gap separating the chunks, if any."""
    spans = sorted((c.bbox[0], c.bbox[2]) for c in chunks)
    best, best_width, right = None, MIN_GUTTER, spans[0][1]
    for x0, x1 in spans[1:]:
        if x0 - right >= best_width:
            best, best_width = (x0 + right) / 2, x0 - right
        right = max(right, x1)
    return best


def reading_order(chunks: list[Chunk], notes: list[str] | None = None) -> list[Chunk]:
    """Recursive column cut. Full-width chunks split the page into bands; columns read left to right.

    `notes` receives a remark when a region had to be read top to bottom although its text
    blocks overlap sideways without a clear column structure.
    """
    if len(chunks) <= 1:
        return list(chunks)
    cut = _gutter(chunks)
    if cut is not None:
        left = [c for c in chunks if c.bbox[2] <= cut]
        right = [c for c in chunks if c.bbox[2] > cut]
        return reading_order(left, notes) + reading_order(right, notes)

    # No clean gutter: something spans the columns. Full-width chunks (titles, wide
    # paragraphs, a band with a different column count) cut the region into horizontal bands.
    region = max(c.bbox[2] for c in chunks) - min(c.bbox[0] for c in chunks)
    spanning = [c for c in chunks if c.width >= 0.6 * region]
    if not spanning or len(spanning) == len(chunks):
        narrow = [c for c in chunks if c.width < 0.6 * region]
        cut = _gutter(narrow) if len(narrow) >= 2 else None
        spanning = [c for c in chunks if cut is not None and c.bbox[0] < cut < c.bbox[2]]
    if spanning and len(spanning) < len(chunks):
        spanning.sort(key=lambda c: c.bbox[1])
        rest = [c for c in chunks if c not in spanning]
        ordered: list[Chunk] = []
        for s in spanning:
            # read before `s`: everything above it, and anything standing to its left on the same rows
            above = [c for c in rest if (c.bbox[1] + c.bbox[3]) / 2 < s.bbox[1]
                     or (c.bbox[2] <= s.bbox[0] and c.bbox[1] < s.bbox[3] and c.bbox[3] > s.bbox[1])]
            rest = [c for c in rest if c not in above]
            ordered += reading_order(above, notes) + [s]
        return ordered + reading_order(rest, notes)
    body = [c for c in chunks if c.type not in FIXED_TYPES and not c.detection and c.lines >= 2
            and not is_caption(c)]
    if notes is not None and len(body) >= 3 and any(
            min(a.bbox[3], b.bbox[3]) - max(a.bbox[1], b.bbox[1]) > 0.5 * min(a.bbox[3] - a.bbox[1],
                                                                                b.bbox[3] - b.bbox[1])
            for i, a in enumerate(body) for b in body[i + 1:]):
        notes.append("interleaved")            # paragraphs side by side, yet no gutter between them
    return sorted(chunks, key=lambda c: (round(c.bbox[1], 1), c.bbox[0]))


def count_columns(chunks: list[Chunk]) -> int:
    """Largest number of side-by-side text columns on the page."""
    body = [c for c in chunks if c.lines >= 2 and c.type not in FIXED_TYPES]
    if len(body) < 2:
        return 1
    region = max(c.bbox[2] for c in body) - min(c.bbox[0] for c in body)
    narrow = sorted((c.bbox[0], c.bbox[2]) for c in body if c.width < 0.6 * region)
    if not narrow:
        return 1
    columns, right = 1, narrow[0][1]
    for x0, x1 in narrow[1:]:
        if x0 - right >= MIN_GUTTER:
            columns += 1
        right = max(right, x1)
    return columns


# ----------------------------------------------------------------------------- classification


def body_font_size(chunks: list[Chunk]) -> float:
    weights: Counter[float] = Counter()
    for c in chunks:
        weights[round(c.size * 2) / 2] += len(c.text)
    return weights.most_common(1)[0][0] if weights else 10.0


def _is_equation(c: Chunk) -> bool:
    text = re.sub(r"</?su[pb]>", " ", c.text)
    return (c.lines == 1 and "=" in text and len(text) < 100 and not text.rstrip().endswith(".")
            and len(re.findall(r"[A-Za-z]{4,}", text)) < 4)


def classify(chunks: list[Chunk], pages: dict[int, PageInfo], body: float) -> None:
    in_references = False
    for c in chunks:
        if c.type in FIXED_TYPES or c.detection:     # found from the page geometry
            continue
        text = c.text
        if c.items:
            c.type = "reference" if in_references else "list"
            continue
        short = len(text) < 200 and c.lines <= 3
        bigger = c.size >= body + HEADING_DELTA
        bold_head = c.bold and c.size >= body - 0.3 and len(text) < 120 and c.lines <= 2
        if is_caption(c):
            c.type = "caption"
        elif re.fullmatch(r"\d{1,3}", text.strip()) and c.size >= body + 4:
            c.type = "chapter_number"
        elif short and (bigger or bold_head) and not text.rstrip().endswith((".", ",", ";")) \
                and re.search(r"[A-Za-z]", text):
            c.type = "heading"
            in_references = bool(REFERENCES_HEADING_RE.match(text))
        elif (c.size <= body - 1.5 and c.bbox[1] >= pages[c.page].height * 0.8
              and not (in_references and re.match(r"^\[?\d", text))):
            c.type = "footnote"
        elif len(text) < 400 and re.match(r"^Abbreviations?\s*:", text):
            c.type = "footnote"                      # the key printed under a figure or table
        elif in_references or REFERENCE_RE.match(text):
            c.type = "reference"
        elif _is_equation(c):
            c.type = "equation"
        elif BULLET_RE.match(text):
            c.type = "list_item"
        else:
            c.type = "paragraph"
    # Heading level = rank of the heading's style. Clearly larger type ranks higher; styles of
    # about the same size rank in the order the document first uses them.
    styles: list[tuple[str, float]] = []
    for c in chunks:
        style = (c.font, round(c.size * 2) / 2)
        if c.type == "heading" and style not in styles:
            styles.append(style)
    styles.sort(key=lambda st: -int(st[1]))          # stable: first appearance breaks ties
    for c in chunks:
        c.level = min(styles.index((c.font, round(c.size * 2) / 2)) + 1, 4) if c.type == "heading" else None


def link_captions(chunks: list[Chunk], body: float, *, keep_order: bool = False) -> list[Chunk]:
    """Attach tables/figures to their captions and notes, and keep each group together in the order.

    With `keep_order` the links are made but nothing moves (the order was decided by the model).
    """
    captions = [c for c in chunks if c.type == "caption"]
    taken: set[str] = set()
    out = list(chunks)

    def move(chunk: Chunk, anchor: Chunk, after: bool) -> None:
        if keep_order:
            return
        out.remove(chunk)
        out.insert(out.index(anchor) + (1 if after else 0), chunk)

    by_id = {c.id: c for c in chunks}
    taken.update(c.caption_id for c in chunks if c.type in OBJECT_TYPES and c.caption_id)
    for obj in (c for c in chunks if c.type in OBJECT_TYPES):
        best, best_gap = by_id.get(obj.caption_id), 80.0     # set already when the caption heads a panel
        for cap in captions if best is None else ():
            is_table_caption = cap.text.lower().startswith("table")
            if cap.id in taken or is_table_caption != (obj.type == "table"):
                continue
            overlap = min(cap.bbox[2], obj.bbox[2]) - max(cap.bbox[0], obj.bbox[0])
            if overlap < 0.3 * min(cap.width, obj.width):
                continue
            gap = max(cap.bbox[1] - obj.bbox[3], obj.bbox[1] - cap.bbox[3], 0.0)
            if gap < best_gap:
                best, best_gap = cap, gap
        if best is not None:
            obj.caption_id = best.id
            taken.add(best.id)
            number = re.search(r"\d+[\w.\-]*", best.text)
            obj.label = number.group(0).rstrip(".") if number else None
            move(best, obj, after=best.bbox[1] > obj.bbox[1])
        if obj.type != "table":
            continue
        for note in chunks:                      # a note printed directly under the table
            gap = note.bbox[1] - obj.bbox[3]
            overlap = min(note.bbox[2], obj.bbox[2]) - max(note.bbox[0], obj.bbox[0])
            if (note.type in ("paragraph", "footnote") and -2 <= gap <= 16 and overlap > 0.3 * note.width
                    and (note.size <= body - 0.8 or NOTE_RE.match(note.text))):
                note.type, note.type_locked, note.note_of = "footnote", True, obj.id
                anchor = out[out.index(obj) + 1] if obj.caption_id and best is not None and \
                    best.bbox[1] > obj.bbox[1] else obj
                move(note, anchor, after=True)
                break
    return out


def link_continued_tables(chunks: list[Chunk]) -> None:
    """A table whose caption number was already used continues the earlier table."""
    first: dict[str, Chunk] = {}
    for t in (c for c in chunks if c.type == "table" and c.label):
        head = first.setdefault(t.label, t)
        if head is t or not t.rows or not head.rows:
            continue
        t.continues = head.id
        repeated = 0
        for mine, theirs in zip(t.rows, head.rows, strict=False):
            if mine != theirs or repeated >= max(head.header_rows, 1):
                break
            repeated += 1
        t.repeated_header_rows = repeated


# ----------------------------------------------------------------------------- continuation merge


def _column_of(c: Chunk) -> tuple[int, int]:
    return c.page, round(c.bbox[0] / 20)


def _continues_list(head: Chunk, nxt: Chunk) -> bool:
    """Numbered steps that count on (… 6 | 7 …), or bullets carried over into the next column."""
    if not head.items or not nxt.items:
        return False
    last, first = STEP_RE.match(head.items[-1]), STEP_RE.match(nxt.items[0])
    if last and first:
        return int(first.group(1)) == int(last.group(1)) + 1
    moved = _column_of(nxt) != _column_of(head)
    return bool(moved and DOT_BULLET_RE.match(head.items[-1]) and DOT_BULLET_RE.match(nxt.items[0]))


def merge_continuations(chunks: list[Chunk]) -> list[Chunk]:
    """Join a paragraph that breaks at the end of a column or page with its continuation."""
    out: list[Chunk] = []
    last_para: Chunk | None = None
    for c in chunks:
        if c.type == "list" and out and out[-1].type == "list" and _continues_list(out[-1], c):
            head = out[-1]
            head.items += c.items
            head.text = "\n".join(head.items)
            head.parts.append((c.page, c.bbox))
            head.lines += c.lines
            continue
        if c.type == "paragraph" and last_para is not None:
            prev_text = re.sub(r"<sup>.*?</sup>$", "", last_para.text).rstrip()
            moved = _column_of(c) != _column_of(last_para)
            if moved and not prev_text.endswith(SENTENCE_END) and c.text[:1].islower():
                last_para.text = join_lines([last_para.text, c.text])
                last_para.parts.append((c.page, c.bbox))
                last_para.lines += c.lines
                continue
        out.append(c)
        if c.type == "paragraph":
            last_para = c
        elif c.type not in ("caption", "footnote", "table", "figure", "callout"):
            last_para = None        # a heading, list or reference ends the paragraph for good
    return out


# ----------------------------------------------------------------------------- structure review


def review_structure(parsed: ParsedDocument) -> None:
    """Record what the parser is unsure about, so it is seen before any paid translation.

    error   = the stored structure is known to be damaged (translation is held back)
    warning = plausible, but a person should look at the page
    """
    linked = {c.caption_id for c in parsed.chunks if c.caption_id}
    for c in parsed.chunks:
        if c.type == "table":
            rows = c.rows or []
            cells = [cell for row in rows for cell in row]
            occupied = {(r, col) for r, row in enumerate(rows) for col, cell in enumerate(row) if cell}
            for span in c.spans:
                r0, r1 = span["row_start"], span["row_end"]
                c0, c1 = span["col_start"], span["col_end"]
                if (r0, c0) in occupied:
                    occupied.update((r, col) for r in range(max(0, r0), min(len(rows), r1 + 1))
                                    for col in range(max(0, c0), min(len(rows[r]), c1 + 1)))
            full = sum(sum((r, col) in occupied for col in range(len(row))) >= 2
                       for r, row in enumerate(rows))
            broken = [cell for cell in cells if re.fullmatch(r"[()\[\]?]+", cell.strip())
                      or cell.count("(") != cell.count(")")]       # text cut through the middle
            if rows and (full < 0.6 * len(rows) or len(broken) >= 2):
                parsed.report(c.page, "table_structure", "error",
                              f"table {c.label or ''} has an irregular grid ({full} of {len(rows)} rows are complete); "
                              "it may be a drawing or its cells may be misaligned".replace("  ", " "))
            elif c.caption_id is None:
                parsed.report(c.page, "table_without_caption", "warning",
                              f"a table of {len(rows)} rows has no caption; check that it is a table")
        elif c.type == "figure":
            if c.caption_id is None:
                parsed.report(c.page, "figure_without_caption", "warning", "a figure has no caption")
            wordy = [t for t in c.labels if re.search(r"[A-Za-z]{3}", t)]
            if len(wordy) >= 12:
                parsed.report(c.page, "figure_labels", "warning",
                              f"figure {c.label or ''} carries {len(wordy)} text labels; check how they were grouped"
                              .replace("  ", " "))
        elif c.type == "equation" and c.detection and c.extra.get("verified"):
            parsed.report(c.page, "formula_text", "warning",
                          f"a formula was written out by the model and will be translated; check it: {c.text[:120]}")
        elif c.type == "equation" and c.detection:
            parsed.report(c.page, "formula_image", "warning",
                          "a formula is kept as an image of the source and is not translated; "
                          f"check it: {c.text[:120]}")
        elif c.type == "caption" and c.id not in linked:
            kind = "table" if c.text.lower().startswith("table") else "figure"
            parsed.report(c.page, f"{kind}_not_found", "error" if kind == "table" else "warning",
                          f"caption \"{c.text[:60]}\" has no {kind} attached; its content may be in the text flow")
        elif c.type in ("paragraph", "list_item", "other") and not c.items:
            text = re.sub(r"</?su[pb]>", "", c.text).strip()
            if text and len(text) <= 4 and not re.search(r"[A-Za-z0-9]", text):
                parsed.report(c.page, "stray_symbols", "error",
                              f"loose symbols \"{text}\" stand as a paragraph; a formula or label was probably split")


# ----------------------------------------------------------------------------- chapters


def find_chapters(doc: pymupdf.Document, chunks: list[Chunk], page_count: int) -> list[ChapterSpan]:
    def first_chunk_on_or_after(page: int) -> int:
        return next((i for i, c in enumerate(chunks) if c.page >= page), len(chunks))

    starts: list[tuple[str, int, int]] = []          # title, page, chunk index
    toc = doc.get_toc(simple=True)
    if toc:
        top = min(level for level, _, _ in toc)
        entries = [(title.strip(), page) for level, title, page in toc if level == top and page >= 1]
        if len(entries) >= 2:
            starts = [(t, p, first_chunk_on_or_after(p)) for t, p in entries]
    if not starts:
        heads = [(i, c) for i, c in enumerate(chunks) if c.type == "heading"]
        named = [(i, c) for i, c in heads if CHAPTER_RE.match(c.text)]
        chosen = named if len(named) >= 2 else []
        for n, (i, c) in enumerate(chosen):
            title = c.text
            nxt = chunks[i + 1] if i + 1 < len(chunks) else None
            if nxt is not None and nxt.type == "heading" and len(c.text) < 20 and n < len(chosen):
                title = f"{c.text}: {nxt.text}"      # "CHAPTER 7" followed by the real title
            starts.append((title, c.page, i))
    if not starts:
        for i, c in enumerate(chunks):               # a large chapter numeral next to the title
            if c.type == "chapter_number":
                near = [h for h in chunks[max(0, i - 1): i + 3] if h.type == "heading" and h.page == c.page]
                title = f"{c.text.strip()} {near[0].text}" if near else f"Chapter {c.text.strip()}"
                starts.append((title, c.page, max(0, i - 1) if near and chunks[i - 1] is near[0] else i))
    if not starts:
        starts = [("Document", 1, 0)]
    elif starts[0][2] > 0:
        starts.insert(0, ("Front matter", 1, 0))

    starts = [s for n, s in enumerate(starts) if n == 0 or s[2] > starts[n - 1][2]]
    chapters = []
    for n, (title, page, idx) in enumerate(starts):
        nxt = starts[n + 1] if n + 1 < len(starts) else None
        end = max(page, chunks[nxt[2] - 1].page) if nxt else page_count
        chapters.append(ChapterSpan(title=title[:500], page_start=page, page_end=end, first_chunk=idx))
    return chapters


# ----------------------------------------------------------------------------- entry point


def analyse(doc: pymupdf.Document, reorder=None, *, every_page: bool = False) -> ParsedDocument:
    """Parse an open PDF. `reorder(page, info, chunks) -> chunks | None` is the model step.

    The model is asked about complex pages (figures, tables, captions, formulas, 3+ columns) or,
    with `every_page`, about every page that has more than one chunk.
    """
    pages: list[PageInfo] = []
    per_page: list[list[Chunk]] = []
    for number, page in enumerate(doc, start=1):
        info, chunks = extract_page(page, number)
        pages.append(info)
        per_page.append(chunks)

    warnings: list[str] = []
    scanned = [p.number for p in pages if p.scanned]
    if scanned:
        shown = ", ".join(map(str, scanned[:10])) + ("…" if len(scanned) > 10 else "")
        warnings.append(f"{len(scanned)} page(s) have no text layer and need OCR (not supported yet): {shown}")

    dropped = drop_running_heads(pages, per_page)
    everything = [c for chunks in per_page for c in chunks]
    body = body_font_size(everything)
    by_number = {p.number: p for p in pages}

    ordered: list[Chunk] = []
    uncertain_order: list[int] = []
    for info, chunks, page in zip(pages, per_page, doc, strict=True):
        info.columns = count_columns(chunks)
        notes: list[str] = []
        page_chunks = reading_order(chunks, notes)
        classify(page_chunks, by_number, body)
        page_chunks = link_captions(page_chunks, body)
        info.complex = bool(
            info.columns > 2 or info.tables or info.figures
            or any(c.type in ("caption", "callout") or c.detection == "fraction" for c in page_chunks)
        )
        if reorder is not None and (info.complex or every_page) and len(page_chunks) > 1:
            try:
                improved = reorder(page, info, page_chunks)
            except Exception as exc:  # noqa: BLE001 - the deterministic order is always a valid fallback
                improved = None
                warnings.append(f"page {info.number}: structure model failed ({type(exc).__name__}: {exc})")
            if improved is not None:
                dropped += sum(c.dropped for c in page_chunks)
                page_chunks = link_captions(improved, body, keep_order=True)   # a re-typed figure finds its caption
                info.tables = sum(c.type == "table" for c in page_chunks)
                info.figures = sum(c.type == "figure" for c in page_chunks)
                warnings += [f"p{info.number}: {note}" for note in info.model_notes]
                notes.clear()       # the accepted order replaces the uncertain deterministic fallback
        if notes:
            uncertain_order.append(info.number)
        info.chunk_ids = [c.id for c in page_chunks]
        ordered += page_chunks

    # heading levels and the references section are document-wide, so classify once more in order
    locked = {c.id: c.type for c in ordered if c.type_locked}
    classify(ordered, by_number, body)
    for c in ordered:
        if c.id in locked:
            c.type = locked[c.id]
            c.level = (c.level or 3) if c.type == "heading" else None
    merged = merge_continuations(ordered)
    link_continued_tables(merged)
    chapters = find_chapters(doc, merged, len(pages))
    parsed = ParsedDocument(pages=pages, chunks=merged, chapters=chapters, body_size=body,
                            dropped=dropped, warnings=warnings)
    for number in scanned:
        parsed.issues.append({"page": number, "code": "scanned", "severity": "warning",
                              "message": "no text layer; the page needs OCR and is not translated"})
    for number in uncertain_order:
        if by_number[number].read_by_model:
            continue                                  # the model saw the page and set the order
        parsed.report(number, "reading_order", "error",
                      "text columns could not be separated; the reading order is a guess")
    review_structure(parsed)
    return parsed


def analyse_bytes(data: bytes, reorder=None, *, every_page: bool = False) -> ParsedDocument:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return analyse(doc, reorder, every_page=every_page)
