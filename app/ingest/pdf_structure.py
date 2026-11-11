"""Model step: the model reads the page image and decides the page's structure, never its text.

The model sees the page image plus the chunks the geometric parser proposed (ids, text, boxes,
guessed types) and answers with ids only:

  order / drop / types   reading order, page furniture, corrected text types
  figure_labels          loose text that is really a label inside a figure
  not_tables             a "table" that is really a drawing with labels
  merge                  pieces of one paragraph or label that were split
  table_rows             the cells of a table arranged into the right rows
  formulas               the pieces of a formula, with a one-line transcription

Nothing the model writes replaces source text unless it can be checked against the text
layer: an answer must use every id exactly once, a re-arranged table must contain exactly
the same words and numbers as before, and a formula transcription must contain exactly the
words and numbers of its pieces. What fails a check is ignored; the parser's proposal stands.
"""
from __future__ import annotations

import base64
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

from app.ingest.pdf_layout import Chunk, PageInfo, _union, join_lines
from app.llm.gateway import LLMGateway

ALLOWED_TYPES = {"heading", "paragraph", "list_item", "caption", "footnote", "reference", "other"}
PREVIEW_CHARS = 160
# found from the page geometry; the model may move these but not rename them through "types"
STRUCTURAL_TYPES = {"table", "figure", "callout", "list", "equation", "chapter_number"}
TOKEN_RE = re.compile(r"[A-Za-z]+|\d+")

SYSTEM = """You analyse the layout of one page of an English medical textbook.
You receive the page image and a JSON list of chunks a parser extracted from it, each with an
id, its text (or a preview), its bounding box [x0, y0, x1, y1] in points and a guessed type.
The parser reads geometry only; you can see the page. Correct its structure.

Return ONLY JSON:
{"order": ["id", ...], "drop": ["id", ...], "types": {"id": "type"},
 "figure_labels": {"figure id": ["id", ...]}, "not_tables": ["table id", ...],
 "merge": [["id", "id", ...]], "table_rows": {"table id": [["cell", ...], ...]},
 "formulas": [{"ids": ["id", ...], "text": "..."}]}
Every key except "order" may be left out.

- "order": ids in human reading order (a column top to bottom, then the next column; a figure
  or table next to its caption; boxes, captions and footnotes after the paragraph they interrupt).
- "drop": page furniture only: running headers, footers, page numbers, watermarks.
- "types": only for text ids whose guessed type is wrong. Allowed: heading, paragraph, list_item,
  caption, footnote, reference, other.
- "figure_labels": text chunks that are printed inside a figure as its labels (leader-line
  labels, axis titles, panel letters), listed under that figure's id.
- "not_tables": chunks typed "table" that are really a drawing or diagram with labels.
- "merge": chunks that are pieces of ONE paragraph, heading or label, in reading order.
- "table_rows": only if a table's cells are in the wrong rows or columns: the same cell texts,
  character for character, arranged correctly. Do not reword, add or remove anything.
- "formulas": a displayed formula. "ids" are the chunks that make it up (one id if it is already
  one chunk); "text" is the formula on one line, using exactly the words and numbers printed,
  with ( ) [ ] / = + − × for its structure, e.g. "ABPI = (ankle pressure) / (brachial pressure)".

Rules: every id appears exactly once: in "order", in "drop", in a "figure_labels" list, or as a
later member of a "merge" group or of a formula's "ids". The first id of a merge group and of a
formula stays in "order". Never invent ids. Apart from "table_rows" and formula "text", return no
chunk text."""


class StructureError(ValueError):
    pass


@dataclass
class Plan:
    order: list[str]
    types: dict[str, str]
    drop: list[str]
    figure_labels: dict[str, list[str]] = field(default_factory=dict)
    not_tables: list[str] = field(default_factory=list)
    merge: list[list[str]] = field(default_factory=list)
    table_rows: dict[str, list[list[str]]] = field(default_factory=dict)
    formulas: list[dict] = field(default_factory=list)

    def __iter__(self):                       # (order, types, drop), as the first version returned
        return iter((self.order, self.types, self.drop))

    def __getitem__(self, index: int):
        return (self.order, self.types, self.drop)[index]


def tokens(text: str) -> Counter:
    """The words and numbers of a text; what a rearrangement must keep exactly."""
    return Counter(TOKEN_RE.findall(re.sub(r"</?su[pb]>", " ", text)))


def _preview(c: Chunk) -> str:
    if c.type == "table":
        return " / ".join(" | ".join(row) for row in c.rows or [])[:1500]
    if c.type == "figure":
        return f"[figure] {' / '.join(c.labels)}"[:PREVIEW_CHARS * 3]
    if c.type == "equation" or len(c.text) <= PREVIEW_CHARS:
        return c.text
    return c.text[:PREVIEW_CHARS] + "…"


def _str_list(value: object, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise StructureError(f"{what} is not a list of ids")
    return value


def validate(answer: object, ids: list[str], kinds: dict[str, str] | None = None) -> Plan:
    """Check that the answer accounts for every chunk exactly once and names only real ids.

    `kinds` maps id -> chunk type; without it only order/drop/types can be checked.
    """
    if not isinstance(answer, dict):
        raise StructureError("answer is not an object")
    order, drop, types = answer.get("order"), answer.get("drop") or [], answer.get("types") or {}
    if not isinstance(order, list) or not isinstance(drop, list) or not isinstance(types, dict):
        raise StructureError("wrong field types")
    kinds = kinds or {}
    figure_labels = answer.get("figure_labels") or {}
    if not isinstance(figure_labels, dict):
        raise StructureError("figure_labels is not an object")
    figure_labels = {k: _str_list(v, "figure_labels") for k, v in figure_labels.items() if v}
    not_tables = _str_list(answer.get("not_tables") or [], "not_tables")
    merge = [_str_list(g, "merge") for g in answer.get("merge") or [] if g]
    formulas = [f for f in answer.get("formulas") or [] if isinstance(f, dict) and f.get("ids")]
    for formula in formulas:
        _str_list(formula["ids"], "formula ids")
    table_rows = answer.get("table_rows") or {}
    if not isinstance(table_rows, dict):
        raise StructureError("table_rows is not an object")

    consumed = [i for labels in figure_labels.values() for i in labels]
    consumed += [i for group in merge for i in group[1:]]
    consumed += [i for formula in formulas for i in formula["ids"][1:]]
    used = order + drop + consumed
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

    def text_only(group: list[str], what: str) -> None:
        wrong = [i for i in group if kinds.get(i) in ("table", "figure", "callout")]
        if wrong:
            raise StructureError(f"{what} may hold text chunks only, not {wrong}")

    for figure, labels in figure_labels.items():
        if figure not in order or kinds.get(figure, "figure") != "figure" and figure not in not_tables:
            raise StructureError(f"figure_labels names {figure}, which is not a figure in the order")
        text_only(labels, "figure_labels")
    for table in not_tables:
        if table not in order or kinds.get(table, "table") != "table":
            raise StructureError(f"not_tables names {table}, which is not a table in the order")
    for group in merge:
        if len(group) < 2 or group[0] not in order:
            raise StructureError("a merge group needs two ids and its first id in the order")
        text_only(group, "merge")
    for formula in formulas:
        if formula["ids"][0] not in order:
            raise StructureError("the first id of a formula must be in the order")
        text_only(formula["ids"], "formulas")
    for table in table_rows:
        if kinds.get(table, "table") != "table" or table in not_tables:
            raise StructureError(f"table_rows names {table}, which is not a table")
    return Plan(order, types, drop, figure_labels, not_tables, merge, table_rows, formulas)


def apply(plan: Plan, chunks: list[Chunk]) -> tuple[list[Chunk], list[str]]:
    """Carry out a validated plan. Returns the page's chunks and notes on what was not accepted."""
    by_id = {c.id: c for c in chunks}
    notes: list[str] = []

    for group in plan.merge:                          # pieces of one paragraph or label
        head, rest = by_id[group[0]], [by_id[i] for i in group[1:]]
        if head.items or any(c.items for c in rest):
            head.items = [item for c in [head] + rest for item in (c.items or [c.text])]
            head.text = "\n".join(head.items)
        else:
            head.text = join_lines([head.text] + [c.text for c in rest])
        head.bbox = _union([head.bbox] + [c.bbox for c in rest])
        head.lines += sum(c.lines for c in rest)

    for table_id in plan.not_tables:                  # a drawing that was read as a table
        table = by_id[table_id]
        table.labels = [cell for row in table.rows or [] for cell in row if cell]
        table.label_boxes = [table.bbox] * len(table.labels)
        table.type, table.rows, table.spans, table.header_rows = "figure", None, [], 0
        table.caption_id, table.detection = None, "model"

    for figure_id, labels in plan.figure_labels.items():
        figure = by_id[figure_id]
        for label in (by_id[i] for i in labels):
            for item in label.items or [label.text]:
                figure.labels.append(item)
                figure.label_boxes.append(label.bbox)
            figure.bbox = _union([figure.bbox, label.bbox])

    for table_id, rows in plan.table_rows.items():
        table = by_id[table_id]
        ok = (isinstance(rows, list) and len(rows) >= 2
              and all(isinstance(r, list) and all(isinstance(c, str) for c in r) for r in rows))
        if ok and tokens(" ".join(c for r in rows for c in r)) == tokens(" ".join(c for r in table.rows for c in r)):
            width = max(len(r) for r in rows)
            table.rows = [r + [""] * (width - len(r)) for r in rows]
            table.spans, table.lines = [], len(rows)
            table.header_rows = min(table.header_rows, 1)
            table.extra["rows_by"] = "model"
        else:
            notes.append(f"table {table.label or table_id}: the model's rows do not hold the same text; kept as parsed")

    for formula in plan.formulas:
        parts = [by_id[i] for i in formula["ids"]]
        head = parts[0]
        source = " ".join(c.text for c in parts)
        text = formula.get("text")
        head.bbox = _union([c.bbox for c in parts])
        head.extra.setdefault("source", source)
        if head.type != "equation" or not head.detection:
            head.type, head.detection, head.items = "equation", "model", []
        if isinstance(text, str) and text.strip() and tokens(text) == tokens(source):
            head.text, head.extra["verified"] = text.strip(), True
        else:
            if len(parts) > 1:
                head.text = source
            notes.append("a formula's transcription does not match its printed words and numbers; kept as an image")

    for chunk_id, kind in plan.types.items():
        chunk = by_id[chunk_id]
        if chunk_id in plan.order and chunk.type not in STRUCTURAL_TYPES:
            chunk.type, chunk.type_locked = kind, True
    for chunk_id in plan.drop:
        by_id[chunk_id].dropped = True
    return [by_id[i] for i in plan.order], notes


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
        plan = validate(result.data, [c.id for c in chunks], {c.id: c.type for c in chunks})
        out, notes = apply(plan, chunks)
        info.model_notes = notes
        info.read_by_model = True
        return out

    return reorder
