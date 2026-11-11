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

Tables and figures are handled in PR-4b; here image areas only mark a page as complex.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

BBox = tuple[float, float, float, float]

LIGATURES = str.maketrans({"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "­": ""})
CAPTION_RE = re.compile(r"^(Figure|Fig\.?|Table|Box|Algorithm)\s+\d+[\w.\-]*", re.I)
REFERENCE_RE = re.compile(r"^\[?\d{1,3}[\].]\s+[A-Z][\w'’\-]+,?\s+[A-Z]{1,3}\b")
REFERENCES_HEADING_RE = re.compile(r"^(selected |key )?(references|bibliography)\b", re.I)
CHAPTER_RE = re.compile(r"^(chapter|section|part)\s+(\d+|[ivxlc]+)\b", re.I)
BULLET_RE = re.compile(r"^[•▪■●◦‣–-]\s+")
PAGE_NUMBER_RE = re.compile(r"^(page\s+)?(\d{1,4}|[ivxlcdm]{1,7})$", re.I)
SENTENCE_END = (".", "?", "!", ":", ";", "”", '"', ")", "]")

MARGIN_BAND = 0.09      # top/bottom share of the page where running heads live
MIN_GUTTER = 9.0        # points of clear horizontal space that count as a column gap
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
    chunk_ids: list[str] = field(default_factory=list)


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


# ----------------------------------------------------------------------------- extraction


def _line_text(line: dict) -> tuple[str, float, bool]:
    """One PDF line -> (text with <sup> markup, body font size, is bold)."""
    out: list[str] = []
    sizes: list[tuple[float, int]] = []
    bold_chars = total = 0
    for span in line["spans"]:
        text = span["text"].translate(LIGATURES)
        if not text:
            continue
        if span["flags"] & 1 and len(text.strip()) <= 12 and text.strip():
            # superscript: citation markers and exponents must not fuse with the word before
            out.append(f"<sup>{text.strip()}</sup>")
            continue
        out.append(text)
        n = len(text.strip())
        sizes.append((span["size"], n))
        total += n
        if span["flags"] & 16 or "bold" in span["font"].lower():
            bold_chars += n
    text = re.sub(r"[ \t ]+", " ", "".join(out)).strip()
    weight = sum(n for _, n in sizes)
    size = sum(s * n for s, n in sizes) / weight if weight else 0.0
    return text, size, bool(total) and bold_chars / total > 0.6


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


def _union(boxes: list[BBox]) -> BBox:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


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
        group: list[tuple[str, float, bool, BBox]] = []

        def flush(group=group):
            if not group:
                return
            text = join_lines([g[0] for g in group])
            if text:
                chunks.append(Chunk(
                    id=f"p{number}c{len(chunks)}", page=number, bbox=_union([g[3] for g in group]),
                    text=text, size=statistics.fmean(g[1] for g in group),
                    bold=all(g[2] for g in group), lines=len(group),
                ))
            group.clear()

        for line in block.get("lines", []):
            if abs(line["dir"][0] - 1) > 0.01:   # rotated text (margin labels, watermarks)
                continue
            text, size, bold = _line_text(line)
            if not text or not size:
                continue
            if group:
                prev = group[-1]
                size_jump = abs(size - prev[1]) >= SPLIT_DELTA
                bold_end = prev[2] and not bold and len(group) <= 2 and all(g[2] for g in group)
                if size_jump or bold_end:
                    flush()
            group.append((text, size, bold, tuple(line["bbox"])))
        flush()
    info.image_area = min(1.0, image_area / (info.width * info.height))
    info.scanned = sum(len(c.text) for c in chunks) < 20 and info.image_area > 0.5
    return info, chunks


# ----------------------------------------------------------------------------- headers / footers


def _running_key(text: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"<sup>.*?</sup>", "", text)).strip().lower()


def drop_running_heads(pages: list[PageInfo], per_page: list[list[Chunk]]) -> int:
    """Remove page numbers and text repeated in the top/bottom margin across pages."""
    def in_margin(c: Chunk, p: PageInfo) -> bool:
        return c.bbox[3] <= p.height * MARGIN_BAND or c.bbox[1] >= p.height * (1 - MARGIN_BAND)

    seen: Counter[str] = Counter()
    for page, chunks in zip(pages, per_page, strict=True):
        for key in {_running_key(c.text) for c in chunks if in_margin(c, page) and len(c.text) < 150}:
            seen[key] += 1
    threshold = max(2, min(3, len(pages))) if len(pages) < 6 else max(3, len(pages) // 10)
    dropped = 0
    for page, chunks in zip(pages, per_page, strict=True):
        keep = []
        for c in chunks:
            marginal = in_margin(c, page)
            if marginal and (PAGE_NUMBER_RE.match(c.text.strip()) or
                             (len(pages) > 1 and seen[_running_key(c.text)] >= threshold)):
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


def reading_order(chunks: list[Chunk]) -> list[Chunk]:
    """Recursive column cut. Full-width chunks split the page into bands; columns read left to right."""
    if len(chunks) <= 1:
        return list(chunks)
    cut = _gutter(chunks)
    if cut is not None:
        left = [c for c in chunks if c.bbox[2] <= cut]
        right = [c for c in chunks if c.bbox[2] > cut]
        return reading_order(left) + reading_order(right)

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
            above = [c for c in rest if (c.bbox[1] + c.bbox[3]) / 2 < s.bbox[1]]
            rest = [c for c in rest if c not in above]
            ordered += reading_order(above) + [s]
        return ordered + reading_order(rest)
    return sorted(chunks, key=lambda c: (round(c.bbox[1], 1), c.bbox[0]))


def count_columns(chunks: list[Chunk]) -> int:
    """Largest number of side-by-side text columns on the page."""
    body = [c for c in chunks if c.lines >= 2]
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


def classify(chunks: list[Chunk], pages: dict[int, PageInfo], body: float) -> None:
    in_references = False
    for c in chunks:
        text = c.text
        short = len(text) < 200 and c.lines <= 3
        bigger = c.size >= body + HEADING_DELTA
        bold_head = c.bold and c.size >= body - 0.3 and len(text) < 120 and c.lines <= 2
        if CAPTION_RE.match(text):
            c.type = "caption"
        elif short and (bigger or bold_head) and not text.rstrip().endswith((".", ",", ";")):
            c.type = "heading"
            in_references = bool(REFERENCES_HEADING_RE.match(text))
        elif (c.size <= body - 1.5 and c.bbox[1] >= pages[c.page].height * 0.8
              and not (in_references and re.match(r"^\[?\d", text))):
            c.type = "footnote"
        elif in_references or REFERENCE_RE.match(text):
            c.type = "reference"
        elif BULLET_RE.match(text):
            c.type = "list_item"
        else:
            c.type = "paragraph"
    sizes = sorted({round(c.size) for c in chunks if c.type == "heading" and c.size >= body + HEADING_DELTA},
                   reverse=True)
    for c in chunks:
        if c.type == "heading":
            rank = sizes.index(round(c.size)) + 1 if round(c.size) in sizes else len(sizes) + 1
            c.level = min(rank, 4)


# ----------------------------------------------------------------------------- continuation merge


def _column_of(c: Chunk) -> tuple[int, int]:
    return c.page, round(c.bbox[0] / 20)


def merge_continuations(chunks: list[Chunk]) -> list[Chunk]:
    """Join a paragraph that breaks at the end of a column or page with its continuation."""
    out: list[Chunk] = []
    last_para: Chunk | None = None
    for c in chunks:
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
        elif c.type not in ("caption", "footnote"):
            last_para = None        # a heading, list or reference ends the paragraph for good
    return out


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


def analyse(doc: pymupdf.Document, reorder=None) -> ParsedDocument:
    """Parse an open PDF. `reorder(page, info, chunks) -> chunks | None` is the optional model step."""
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
    for info, chunks, page in zip(pages, per_page, doc, strict=True):
        info.columns = count_columns(chunks)
        page_chunks = reading_order(chunks)
        classify(page_chunks, by_number, body)
        info.complex = (
            info.columns > 2 or info.image_area > 0.02 or any(c.type == "caption" for c in page_chunks)
        )
        if reorder is not None and info.complex and len(page_chunks) > 1:
            try:
                improved = reorder(page, info, page_chunks)
            except Exception as exc:  # noqa: BLE001 - the deterministic order is always a valid fallback
                improved = None
                warnings.append(f"page {info.number}: structure model failed ({type(exc).__name__}: {exc})")
            if improved is not None:
                dropped += len(page_chunks) - len(improved)
                page_chunks = improved
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
    chapters = find_chapters(doc, merged, len(pages))
    return ParsedDocument(pages=pages, chunks=merged, chapters=chapters, body_size=body,
                          dropped=dropped, warnings=warnings)


def analyse_bytes(data: bytes, reorder=None) -> ParsedDocument:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return analyse(doc, reorder)
