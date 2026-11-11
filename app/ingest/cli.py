"""Inspect how a PDF is parsed, without touching the database.

    python -m app.ingest.cli book.pdf                 # summary
    python -m app.ingest.cli book.pdf --pages 40-45 --text   # reading order with text
    python -m app.ingest.cli book.pdf --json parsed.json
    python -m app.ingest.cli book.pdf --llm           # also run the model on complex pages
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import pymupdf

from app.ingest.pdf_layout import analyse


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--pages", help="page range to parse, e.g. 40-45")
    ap.add_argument("--text", action="store_true", help="print chunks in reading order")
    ap.add_argument("--json", type=Path, help="write the full parse result here")
    ap.add_argument("--llm", action="store_true", help="use the structure model on complex pages (costs money)")
    args = ap.parse_args()

    doc = pymupdf.open(str(args.pdf))
    if args.pages:
        first, _, last = args.pages.partition("-")
        doc.select(list(range(int(first) - 1, int(last or first))))

    reorder = None
    if args.llm:
        from app.config import get_settings
        from app.db import get_engine
        from app.ingest.pdf_structure import make_reorder
        from app.llm.gateway import LLMGateway
        reorder = make_reorder(LLMGateway(get_engine(), get_settings()))

    parsed = analyse(doc, reorder)
    words = sum(len(c.text.split()) for c in parsed.chunks)
    print(f"pages: {len(parsed.pages)}   chunks: {len(parsed.chunks)}   words: {words}   "
          f"body font: {parsed.body_size}pt   dropped headers/footers: {parsed.dropped}")
    print("types:", dict(Counter(c.type for c in parsed.chunks)))
    print("columns per page:", dict(Counter(p.columns for p in parsed.pages)))
    print(f"complex pages: {sum(p.complex for p in parsed.pages)}   scanned pages: "
          f"{sum(p.scanned for p in parsed.pages)}")
    print(f"chapters: {len(parsed.chapters)}")
    for ch in parsed.chapters[:40]:
        print(f"  p{ch.page_start}-{ch.page_end}  {ch.title[:80]}")
    for w in parsed.warnings:
        print("WARNING:", w)
    if args.text:
        for c in parsed.chunks:
            tag = f"{c.type}{c.level or ''}"
            print(f"\n[p{c.page} {tag}] {c.text}")
    if args.json:
        args.json.write_text(json.dumps(asdict(parsed), ensure_ascii=False, indent=1), encoding="utf-8")
        print("written:", args.json)


if __name__ == "__main__":
    main()
