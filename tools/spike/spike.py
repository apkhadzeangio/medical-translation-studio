#!/usr/bin/env python3
"""Medical Translation Studio — PR-1 spike.

One chapter (DOCX or PDF) -> translate -> medical review -> deterministic QA
-> bilingual DOCX + Georgian DOCX + cost report. Resumable, budget-capped.

  python spike.py chapter.docx --out out --glossary glossary.csv --max-budget 25
  python spike.py chapter.docx --out out --mock        # no API calls
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from docx import Document
from docx.shared import Pt
from docx.table import Table
from docx.text.paragraph import Paragraph

# --------------------------------------------------------------------------- config

BASE_URL = os.getenv("MTS_BASE_URL", "https://api.openai.com/v1")
API_KEYS = [k.strip() for k in os.getenv("MTS_API_KEYS", os.getenv("OPENAI_API_KEY", "")).split(",") if k.strip()]
MODEL = os.getenv("MTS_MODEL", "gpt-6-astra")
REVIEW_MODEL = os.getenv("MTS_REVIEW_MODEL", MODEL)
PRICE_IN = float(os.getenv("MTS_PRICE_IN", "10"))    # USD per 1M input tokens
PRICE_OUT = float(os.getenv("MTS_PRICE_OUT", "50"))  # USD per 1M output tokens
KA_FONT = os.getenv("MTS_FONT", "Sylfaen")

TRANSLATE_SYS = """You are a senior medical translator and vascular surgeon. Translate English medical \
textbook text into Georgian in a formal academic register used in Georgian medical literature.

Rules:
- Input is JSON: {"segments":[{"id":"...","type":"...","en":"..."}]}. Return ONLY JSON:
  {"segments":[{"id":"...","ka":"..."}]} with exactly the same ids, one output per input.
- Translate meaning precisely. Never add, omit, or soften clinical content.
- Keep all numbers, doses, units, percentages, ranges, statistical values, citation markers
  (e.g. [12], 12,13 superscripts), figure/table references and eponyms' Latin spelling unchanged.
- Drug names: use the established Georgian form; keep the Latin name in parentheses on first mention.
- Abbreviations: give the Georgian term with the original English abbreviation in parentheses on
  first mention, then use the abbreviation.
- Segments of type "reference" (bibliography entries) must be returned unchanged.
- Glossary terms are mandatory; use exactly the given Georgian equivalent (inflect as grammar requires).
"""

REVIEW_SYS = """You are an independent medical reviewer (vascular surgery) checking a Georgian translation \
against its English source. Input is JSON: {"segments":[{"id","en","ka"}]}.

Report ONLY segments that need correction: mistranslation, omitted or added clinical content, wrong
number/dose/unit, wrong anatomy or laterality, wrong negation, glossary violation, or clearly
unnatural Georgian medical phrasing. Do not rewrite acceptable segments for style.

Return ONLY JSON: {"edits":[{"id":"...","ka":"<corrected full segment>","severity":"critical|major|minor",
"reason":"<short reason in Georgian>"}]}. If nothing needs correction return {"edits":[]}.
"""

# --------------------------------------------------------------------------- ingestion


def ingest_docx(path: Path) -> list[dict]:
    """DOCX -> ordered blocks. Images are not carried over in the spike."""
    doc = Document(str(path))
    blocks: list[dict] = []
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            text = p.text.strip()
            if not text:
                continue
            style = (p.style.name or "") if p.style is not None else ""
            m = re.match(r"Heading (\d)", style)
            if m:
                blocks.append({"type": "heading", "level": int(m.group(1)), "en": text})
            elif style == "Title":
                blocks.append({"type": "heading", "level": 0, "en": text})
            else:
                blocks.append({"type": "para", "en": text})
        elif tag == "tbl":
            t = Table(child, doc)
            rows = [[c.text.strip() for c in r.cells] for r in t.rows]
            blocks.append({"type": "table", "rows": rows})
    return blocks


def ingest_pdf(path: Path) -> list[dict]:
    """PDF -> text blocks in reading order (best effort; tables come out as text)."""
    import pymupdf as fitz

    blocks: list[dict] = []
    with fitz.open(str(path)) as pdf:
        sizes = Counter()
        pages = []
        for page in pdf:
            d = page.get_text("dict", sort=True)
            pages.append(d)
            for b in d["blocks"]:
                for line in b.get("lines", []):
                    for s in line["spans"]:
                        sizes[round(s["size"])] += len(s["text"])
        body = sizes.most_common(1)[0][0] if sizes else 10
        for d in pages:
            for b in d["blocks"]:
                if b.get("type") != 0:
                    continue
                spans = [s for line in b["lines"] for s in line["spans"]]
                text = " ".join(
                    "".join(s["text"] for s in line["spans"]).strip() for line in b["lines"]
                )
                text = re.sub(r"(\w)- (\w)", r"\1\2", re.sub(r"\s+", " ", text)).strip()
                if len(text) < 2:
                    continue
                size = max((s["size"] for s in spans), default=body)
                if size >= body + 1.5 and len(text) < 200:
                    blocks.append({"type": "heading", "level": 1 if size >= body + 4 else 2, "en": text})
                else:
                    blocks.append({"type": "para", "en": text})
    return blocks


REF_RE = re.compile(r"^\s*\[?\d{1,3}[\].]\s+[A-Z][\w'\-]+ [A-Z]{1,3}[,.]")


def to_segments(blocks: list[dict]) -> list[dict]:
    """Flatten blocks into translatable segments with stable ids."""
    segs: list[dict] = []
    in_refs = False
    for bi, b in enumerate(blocks):
        if b["type"] == "table":
            for ri, row in enumerate(b["rows"]):
                for ci, cell in enumerate(row):
                    if cell:
                        segs.append({"id": f"b{bi}r{ri}c{ci}", "type": "table_cell", "en": cell})
            continue
        if b["type"] == "heading":
            in_refs = bool(re.match(r"(?i)^(selected )?(key )?references\b", b["en"]))
        kind = b["type"]
        if kind == "para" and (in_refs or REF_RE.match(b["en"])):
            kind = "reference"
        segs.append({"id": f"b{bi}", "type": kind, "en": b["en"]})
    return segs


def chunk(segs: list[dict], max_words: int) -> list[list[dict]]:
    chunks, cur, n = [], [], 0
    for s in segs:
        w = len(s["en"].split())
        if cur and n + w > max_words:
            chunks.append(cur)
            cur, n = [], 0
        cur.append(s)
        n += w
    if cur:
        chunks.append(cur)
    return chunks


# --------------------------------------------------------------------------- glossary


def load_glossary(path: Path | None) -> list[tuple[str, str]]:
    if not path:
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.reader(f) if len(r) >= 2 and r[0].strip()]
    if rows and rows[0][0].strip().lower() in {"en", "english", "term"}:
        rows = rows[1:]
    return [(r[0].strip(), r[1].strip()) for r in rows]


def glossary_for(text: str, glossary: list[tuple[str, str]]) -> list[tuple[str, str]]:
    low = text.lower()
    return [(en, ka) for en, ka in glossary if re.search(r"\b" + re.escape(en.lower()), low)]


# --------------------------------------------------------------------------- LLM


class BudgetExceeded(RuntimeError):
    pass


class LLM:
    def __init__(self, mock: bool, max_budget: float):
        self.mock, self.max_budget = mock, max_budget
        self.lock = threading.Lock()
        self.usage = Counter()
        self.cost = 0.0
        self._i = 0
        self.clients = []
        if not mock:
            from openai import OpenAI

            if not API_KEYS:
                sys.exit("Set MTS_API_KEYS (comma-separated) or OPENAI_API_KEY, or run with --mock.")
            self.clients = [OpenAI(api_key=k, base_url=BASE_URL, timeout=600, max_retries=0) for k in API_KEYS]

    def _client(self):
        with self.lock:
            self._i += 1
            return self.clients[self._i % len(self.clients)]

    def call(self, stage: str, model: str, system: str, payload: dict) -> dict:
        if self.mock:
            return self._mock(stage, payload)
        with self.lock:
            if self.cost >= self.max_budget:
                raise BudgetExceeded(f"budget ${self.max_budget:.2f} reached")
        user = json.dumps(payload, ensure_ascii=False)
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        json_mode = True
        last = None
        for attempt in range(6):
            try:
                kw = {"response_format": {"type": "json_object"}} if json_mode else {}
                r = self._client().chat.completions.create(model=model, messages=msgs, **kw)
                u = r.usage
                pin, pout = (u.prompt_tokens, u.completion_tokens) if u else (0, 0)
                with self.lock:
                    self.usage[f"{stage}_in"] += pin
                    self.usage[f"{stage}_out"] += pout
                    self.usage[f"{stage}_calls"] += 1
                    self.cost += pin / 1e6 * PRICE_IN + pout / 1e6 * PRICE_OUT
                return parse_json(r.choices[0].message.content or "")
            except Exception as e:  # noqa: BLE001 - spike: retry everything retryable
                last = e
                status = getattr(e, "status_code", None)
                if status == 400 and json_mode:
                    json_mode = False  # proxy without JSON mode
                    continue
                if status in (401, 403, 404):
                    raise
                time.sleep(min(60, 2 ** attempt * 2))
        raise RuntimeError(f"{stage}: giving up after retries: {last}")

    @staticmethod
    def _mock(stage: str, payload: dict) -> dict:
        if stage == "translate":
            return {"segments": [{"id": s["id"], "ka": "〔KA〕 " + s["en"]} for s in payload["segments"]]}
        return {"edits": []}


def parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a == -1 or b <= a:
            raise
        return json.loads(text[a : b + 1])


# --------------------------------------------------------------------------- stages


def cached(path: Path, fn):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    out = fn()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return out


def translate_chunk(llm: LLM, ch: list[dict], glossary, prev_tail: str) -> dict:
    todo = [s for s in ch if s["type"] != "reference"]
    out = {s["id"]: s["en"] for s in ch if s["type"] == "reference"}
    if not todo:
        return out
    terms = glossary_for(" ".join(s["en"] for s in todo), glossary)
    payload = {
        "glossary": [{"en": e, "ka": k} for e, k in terms],
        "preceding_context_en": prev_tail,
        "segments": [{"id": s["id"], "type": s["type"], "en": s["en"]} for s in todo],
    }
    for _ in range(2):
        res = llm.call("translate", MODEL, TRANSLATE_SYS, payload)
        got = {x["id"]: x.get("ka", "") for x in res.get("segments", []) if isinstance(x, dict) and "id" in x}
        if all(got.get(s["id"], "").strip() for s in todo):
            out.update({s["id"]: got[s["id"]].strip() for s in todo})
            return out
    missing = [s["id"] for s in todo if not got.get(s["id"], "").strip()]
    raise RuntimeError(f"translation missing segments: {missing[:5]}")


def review_chunk(llm: LLM, ch: list[dict], ka: dict) -> list[dict]:
    todo = [s for s in ch if s["type"] != "reference"]
    if not todo:
        return []
    payload = {"segments": [{"id": s["id"], "en": s["en"], "ka": ka[s["id"]]} for s in todo]}
    res = llm.call("review", REVIEW_MODEL, REVIEW_SYS, payload)
    ids = {s["id"] for s in todo}
    return [e for e in res.get("edits", []) if isinstance(e, dict) and e.get("id") in ids and e.get("ka")]


NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


def nums(text: str) -> Counter:
    return Counter(n.replace(",", ".").rstrip(".") for n in NUM_RE.findall(text))


def qa_segment(seg: dict, ka: str, glossary) -> list[str]:
    issues = []
    a, b = nums(seg["en"]), nums(ka)
    if a != b:
        miss, extra = list((a - b).elements()), list((b - a).elements())
        issues.append(f"რიცხვები არ ემთხვევა: აკლია {miss or '—'}, ზედმეტია {extra or '—'}")
    if seg["type"] != "reference":
        if ka.strip() == seg["en"].strip() and len(seg["en"].split()) > 3:
            issues.append("სეგმენტი უთარგმნელია")
        ratio = len(ka) / max(1, len(seg["en"]))
        if len(seg["en"]) > 80 and not 0.5 <= ratio <= 2.2:
            issues.append(f"საეჭვო სიგრძე (ka/en = {ratio:.2f})")
        for en, k in glossary_for(seg["en"], glossary):
            stem = k[: max(4, len(k) - 2)]  # tolerate Georgian case endings
            if stem.lower() not in ka.lower():
                issues.append(f"ლექსიკონი: «{en}» → «{k}» არ გამოიყენება")
    return issues


# --------------------------------------------------------------------------- export


def _run(par, text: str, bold=False, size=None):
    r = par.add_run(text)
    r.bold = bold
    r.font.name = KA_FONT
    if size:
        r.font.size = Pt(size)
    return r


def export_mono(blocks, ka: dict, path: Path):
    doc = Document()
    doc.styles["Normal"].font.name = KA_FONT
    for bi, b in enumerate(blocks):
        if b["type"] == "heading":
            h = doc.add_heading(level=min(b["level"], 4))
            _run(h, ka.get(f"b{bi}", b["en"]))
        elif b["type"] == "para":
            _run(doc.add_paragraph(), ka.get(f"b{bi}", b["en"]))
        else:
            rows = b["rows"]
            t = doc.add_table(rows=len(rows), cols=max(len(r) for r in rows))
            t.style = "Table Grid"
            for ri, row in enumerate(rows):
                for ci, cell in enumerate(row):
                    par = t.cell(ri, ci).paragraphs[0]
                    _run(par, ka.get(f"b{bi}r{ri}c{ci}", cell), bold=(ri == 0), size=9)
            doc.add_paragraph()
    doc.save(str(path))


def export_bilingual(segs, ka: dict, flags: dict, path: Path):
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = sec.page_height, sec.page_width  # landscape
    doc.styles["Normal"].font.name = KA_FONT
    t = doc.add_table(rows=1, cols=3)
    t.style = "Table Grid"
    for c, title in zip(t.rows[0].cells, ("English", "ქართული", "QA")):
        _run(c.paragraphs[0], title, bold=True, size=9)
    for s in segs:
        row = t.add_row().cells
        head = s["type"] == "heading"
        _run(row[0].paragraphs[0], s["en"], bold=head, size=9)
        _run(row[1].paragraphs[0], ka.get(s["id"], ""), bold=head, size=9)
        _run(row[2].paragraphs[0], "\n".join(flags.get(s["id"], [])), size=7)
    for row in t.rows:
        for c, w in zip(row.cells, (4.3, 4.3, 1.4)):
            c.width = int(w * 914400)
    doc.save(str(path))


# --------------------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path)
    ap.add_argument("--out", type=Path, default=Path("out"))
    ap.add_argument("--glossary", type=Path)
    ap.add_argument("--chunk-words", type=int, default=1200)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-budget", type=float, default=25.0, help="USD hard stop for this run")
    ap.add_argument("--skip-review", action="store_true")
    ap.add_argument("--mock", action="store_true", help="no API calls; tests the plumbing")
    a = ap.parse_args()

    cache = a.out / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    ext = a.input.suffix.lower()
    if ext not in (".docx", ".pdf"):
        sys.exit("Input must be .docx or .pdf")
    blocks = cached(cache / "blocks.json", lambda: (ingest_docx if ext == ".docx" else ingest_pdf)(a.input))
    segs = to_segments(blocks)
    chunks = chunk(segs, a.chunk_words)
    glossary = load_glossary(a.glossary)
    words = sum(len(s["en"].split()) for s in segs)
    print(f"{len(blocks)} blocks, {len(segs)} segments, {words} words, {len(chunks)} chunks, "
          f"{len(glossary)} glossary terms")

    llm = LLM(a.mock, a.max_budget)
    ka: dict[str, str] = {}
    edits: list[dict] = []
    stopped = None

    def do(i: int):
        ch = chunks[i]
        tail = " ".join(s["en"] for s in chunks[i - 1][-2:])[-600:] if i else ""
        tr = cached(cache / f"translate_{i:04d}.json", lambda: translate_chunk(llm, ch, glossary, tail))
        ed = [] if a.skip_review else cached(
            cache / f"review_{i:04d}.json", lambda: review_chunk(llm, ch, tr))
        return tr, ed

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(do, i) for i in range(len(chunks))]
        for i, f in enumerate(futs):
            try:
                tr, ed = f.result()
                ka.update(tr)
                edits.extend(ed)
                print(f"  chunk {i + 1}/{len(chunks)} ok  (${llm.cost:.2f})")
            except BudgetExceeded as e:
                stopped = str(e)
            except Exception as e:  # noqa: BLE001
                print(f"  chunk {i + 1}/{len(chunks)} FAILED: {e}")
                stopped = stopped or f"chunk {i + 1} failed"

    flags: dict[str, list[str]] = {}
    for e in edits:
        ka[e["id"]] = e["ka"].strip()
        flags.setdefault(e["id"], []).append(f"რევიზია [{e.get('severity', '?')}]: {e.get('reason', '')}")
    by_id = {s["id"]: s for s in segs}
    qa_count = 0
    for sid, text in ka.items():
        for issue in qa_segment(by_id[sid], text, glossary):
            flags.setdefault(sid, []).append("QA: " + issue)
            qa_count += 1

    stem = a.input.stem
    export_bilingual(segs, ka, flags, a.out / f"{stem}_bilingual.docx")
    export_mono(blocks, ka, a.out / f"{stem}_ka.docx")
    done_words = sum(len(by_id[i]["en"].split()) for i in ka)
    report = {
        "input": a.input.name, "model": MODEL, "review_model": REVIEW_MODEL, "mock": a.mock,
        "segments": len(segs), "segments_translated": len(ka), "words": words,
        "chunks": len(chunks), "review_edits": len(edits), "qa_flags": qa_count,
        "tokens": dict(llm.usage), "cost_usd": round(llm.cost, 4),
        "cost_per_1000_words_usd": round(llm.cost / done_words * 1000, 4) if done_words else None,
        "seconds": round(time.time() - t0, 1), "stopped": stopped,
        "note": "Cost covers this run only; cached chunks from earlier runs are not re-billed.",
    }
    (a.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if stopped:
        print(f"\nSTOPPED: {stopped}. Re-run the same command to resume.")
        sys.exit(2)


if __name__ == "__main__":
    main()
