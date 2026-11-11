"""Score a parse against a ground-truth JSON (blocks with page, bbox, type, text, optional rows).

    python -m app.ingest.evaluate book.pdf truth.json [--verbose]

The oracle is used for measurement only; nothing in the parser reads it.
Pages whose truth says `text_origin != "native_pdf"` (scans) are skipped.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pymupdf

from app.ingest.pdf_layout import LIGATURES, Chunk, ParsedDocument, analyse

FURNITURE = {"running_header", "running_footer", "page_number", "fixture_notice"}
TYPE_MAP = {
    "heading": {"heading"}, "paragraph": {"paragraph"}, "abstract": {"paragraph"}, "byline": {"paragraph"},
    "note": {"paragraph", "footnote"}, "footnote": {"footnote"}, "table_note": {"footnote"},
    "table_caption": {"caption"}, "figure_caption": {"caption"}, "table": {"table"}, "figure": {"figure"},
    "callout": {"callout"}, "equation": {"equation"}, "ordered_list": {"list"}, "unordered_list": {"list"},
    "reference": {"reference"}, "chapter_number": {"chapter_number"},
}


def norm(text: str) -> str:
    text = re.sub(r"</?su[pb]>", "", text.translate(LIGATURES))
    return re.sub(r"\s+", " ", text).strip()


def _overlap(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    return w * h / min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))


def _truth_text(block: dict) -> str:
    text = block.get("text", "")
    hint = block.get("normalization_hint")
    if hint:
        text = text.replace(hint["display"], hint["semantic"])
    return norm(text)


def evaluate(parsed: ParsedDocument, truth: dict) -> dict:
    native = {p["page"] for p in truth["pages"] if p.get("text_origin", "native_pdf") == "native_pdf"}
    order = {p["page"]: p["reading_order"] for p in truth["pages"]}
    blocks = [b for b in truth["blocks"] if b["page"] in native]
    chunks = [c for c in parsed.chunks if c.page in native]
    issues: list[str] = []
    score = {k: [0, 0] for k in ("furniture", "found", "type", "text", "heading_level", "order", "table_cells",
                                 "table_spans", "figures")}

    def tally(key: str, ok: bool, message: str = "") -> None:
        score[key][1] += 1
        score[key][0] += ok
        if not ok and message:
            issues.append(f"{key}: {message}")

    for b in blocks:
        if b["type"] in FURNITURE:
            want = norm(b["text"])
            leaked = any(c.page == b["page"] and (norm(c.text) == want or (len(want) > 20 and want in norm(c.text)))
                         for c in chunks)
            tally("furniture", not leaked, f"{b['id']} leaked into body: {b['text'][:50]!r}")

    matched: dict[str, Chunk] = {}
    used: set[str] = set()
    for b in (b for b in blocks if b["type"] not in FURNITURE):
        same_page = [c for c in chunks if c.page == b["page"] and c.id not in used]
        want = _truth_text(b)
        best = next((c for c in same_page if want and norm(c.text) == want), None)
        if best is None:
            scored = sorted(((_overlap(c.bbox, b["bbox"]), c) for c in same_page), key=lambda x: -x[0])
            object_like = b["type"] in ("table", "figure")
            best = next((c for s, c in scored if s > 0.5 and (c.type in ("table", "figure")) == object_like), None)
        tally("found", best is not None, f"{b['id']} ({b['type']}) has no counterpart: {b['text'][:60]!r}")
        if best is None:
            continue
        used.add(best.id)
        matched[b["id"]] = best
        allowed = TYPE_MAP.get(b["type"], {b["type"]})
        tally("type", best.type in allowed, f"{b['id']} expected {b['type']}, got {best.type}: {b['text'][:50]!r}")
        if b["type"] == "heading" and best.type == "heading":
            tally("heading_level", best.level == b.get("heading_level"),
                  f"{b['id']} level {b.get('heading_level')} != {best.level}: {b['text'][:40]!r}")
        if b["type"] == "table":
            want_rows = [[norm(c) for c in row] for row in b["rows"]]
            got_rows = [[norm(c) for c in row] for row in best.rows or []]
            same_shape = len(got_rows) == len(want_rows) and all(
                len(g) == len(w) for g, w in zip(got_rows, want_rows, strict=False))
            if not same_shape:
                cols = max((len(r) for r in got_rows), default=0)
                tally("table_cells", False, f"{b['id']} shape {len(got_rows)}x{cols}, expected "
                                            f"{len(want_rows)}x{len(want_rows[0])}")
                score["table_cells"][1] += sum(len(r) for r in want_rows) - 1
            else:
                for r, (g_row, w_row) in enumerate(zip(got_rows, want_rows, strict=True)):
                    for c, (g, w) in enumerate(zip(g_row, w_row, strict=True)):
                        tally("table_cells", g == w, f"{b['id']} cell r{r}c{c}: {g!r} != {w!r}")
            if "spans" in b:
                def key(span: dict) -> tuple:
                    return tuple(span[k] for k in ("row_start", "row_end", "col_start", "col_end"))
                tally("table_spans", sorted(map(key, best.spans)) == sorted(map(key, b["spans"])),
                      f"{b['id']} spans {sorted(map(key, best.spans))} != {sorted(map(key, b['spans']))}")
            if "header_rows" in b:
                tally("table_spans", best.header_rows == b["header_rows"],
                      f"{b['id']} header rows {best.header_rows} != {b['header_rows']}")
        elif b["type"] == "figure":
            tally("figures", _overlap(best.bbox, b["bbox"]) > 0.9, f"{b['id']} bbox {best.bbox} vs {b['bbox']}")
        elif want:
            got = norm(best.text)
            tally("text", got == want, f"{b['id']} text differs:\n      got  {got[:140]!r}\n      want {want[:140]!r}")

    position = {c.id: i for i, c in enumerate(parsed.chunks)}
    for page, ids in order.items():
        seq = [(i, position[matched[i].id]) for i in ids if i in matched]
        for (a, pa), (b_id, pb) in zip(seq, seq[1:], strict=False):
            tally("order", pa < pb, f"page {page}: {a} should come before {b_id}")

    extra = [c for c in chunks if c.id not in used]
    for c in extra:
        issues.append(f"extra: p{c.page} {c.type} {c.text[:60]!r}")
    total = sum(v[0] for v in score.values())
    possible = sum(v[1] for v in score.values())
    return {"score": {k: f"{a}/{b}" for k, (a, b) in score.items()}, "extra_chunks": len(extra),
            "overall": round(100 * total / possible, 1) if possible else 0.0, "issues": issues}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("truth", type=Path)
    ap.add_argument("--verbose", action="store_true", help="list every mismatch")
    args = ap.parse_args()
    result = evaluate(analyse(pymupdf.open(str(args.pdf))), json.loads(args.truth.read_text(encoding="utf-8")))
    for key, value in result["score"].items():
        print(f"{key:14} {value}")
    print(f"{'extra chunks':14} {result['extra_chunks']}")
    print(f"{'overall':14} {result['overall']}%")
    if args.verbose:
        for issue in result["issues"]:
            print(" -", issue)


if __name__ == "__main__":
    main()
