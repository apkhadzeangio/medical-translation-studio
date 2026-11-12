"""Translation stage: plan a chapter into chunk jobs, translate one chunk per job.

A chunk job is safe to run twice: segments that already have a current version are skipped,
so a retry after a crash only pays for what is still missing.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app import jobs
from app.chunking import plan_chunks
from app.config import get_settings
from app.llm.gateway import LLMError, LLMGateway
from app.models import Block, Chapter, Document, Job, QAFlag, Segment, SegmentVersion
from app.pipeline.checks import check_segment
from app.terminology import GlossaryIndex, load_index, tm_add, tm_lookup

# Planning figures per English source word, measured on the first real run (gpt-6-astra,
# 1,879 words, 2026-11-12): translation 4.7 input / 3.9 output tokens, review 8.3 / 0.5.
# One chapter is a small sample; the console shows the real spend as it happens.
TRANSLATE_TOKENS_PER_WORD = (4.7, 3.9)
REVIEW_TOKENS_PER_WORD = (8.3, 0.5)

SYSTEM = """You are a senior medical translator and a vascular surgeon. You translate an English \
medical textbook into Georgian, in the formal academic register of Georgian medical literature.

Input is JSON: {"chapter": "...", "glossary": [{"en","ka"}], "context_before": "...",
"segments": [{"id","kind","en"}]}.
Return ONLY JSON: {"segments": [{"id": "...", "ka": "..."}]} with exactly the same ids, one per input.

Rules
- Translate the meaning precisely. Never add, omit, soften or strengthen clinical content.
  Negation, uncertainty and qualifiers ("not assessed", "not confirmed", "may") stay exactly as strong.
- Copy every number, decimal and thousands separator, unit, range, percentage, inequality sign,
  statistical value and citation marker unchanged. 0.05, 0.5 and 5.0 are different values.
- Keep <sup>…</sup> and <sub>…</sub> tags and their content exactly as given.
- Glossary entries are mandatory: use the given Georgian term (inflect it as grammar requires).
- Drug names: established Georgian form, Latin name in parentheses at first mention.
- Abbreviations: Georgian term with the English abbreviation in parentheses at first mention,
  then the abbreviation alone.
- "context_before" is for understanding only; do not translate it.
- kind "heading": a heading, no final period. kind "table_cell": short, no added words; a cell that
  is only a code, a number or an identifier is returned unchanged. kind "caption": keep the label
  and number ("FIGURE 17.1" → "სურათი 17.1", "TABLE 17.1" → "ცხრილი 17.1").
"""

KIND = {"table": "table_cell", "list": "list_item", "list_item": "list_item"}


@lru_cache
def _gateway(engine: Engine) -> LLMGateway:
    return LLMGateway(engine, get_settings())


def get_gateway(session: Session) -> LLMGateway:
    """One gateway per engine, so the key pool's cooldowns are shared by all jobs in the process."""
    return _gateway(session.get_bind())


def _chunk_key(chapter_id: uuid.UUID, segment_ids: list[uuid.UUID]) -> str:
    digest = hashlib.sha256("".join(map(str, segment_ids)).encode()).hexdigest()[:16]
    return f"translate:{chapter_id}:{digest}"


def plan_chapter(session: Session, chapter_id: uuid.UUID, *, max_words: int | None = None) -> dict[str, Any]:
    """Queue one job per chunk and return what the run is expected to involve."""
    chapter = session.get(Chapter, chapter_id)
    document = session.get(Document, chapter.document_id)
    chunks = plan_chunks(session, chapter_id, max_words or get_settings().translate_chunk_words)
    queued = 0
    for index, chunk in enumerate(chunks):
        job = jobs.enqueue(
            session, "translate_chunk", _chunk_key(chapter_id, chunk.segment_ids),
            {"chapter_id": str(chapter_id), "segment_ids": [str(i) for i in chunk.segment_ids]},
            project_id=document.project_id, priority=100 + index,
        )
        queued += job.status == "queued"
    if chunks:
        chapter.status = "translating"
    words = sum(c.words for c in chunks)
    price = get_gateway(session).prices.get(get_settings().llm_model) or {}
    tokens_in = words * (TRANSLATE_TOKENS_PER_WORD[0] + REVIEW_TOKENS_PER_WORD[0])
    tokens_out = words * (TRANSLATE_TOKENS_PER_WORD[1] + REVIEW_TOKENS_PER_WORD[1])
    estimate = (tokens_in * price.get("input", 0) + tokens_out * price.get("output", 0)) / 1e6
    return {"chapter_id": str(chapter_id), "chunks": len(chunks), "jobs_queued": queued, "words": words,
            "estimated_cost_usd": round(estimate, 2), "estimate_is_rough": True}


def _context_before(session: Session, chapter_id: uuid.UUID, first_block_idx: int) -> str:
    rows = session.execute(
        select(Segment.source_text).join(Block, Block.id == Segment.block_id)
        .where(Segment.chapter_id == chapter_id, Block.idx < first_block_idx,
               Block.type.in_(("paragraph", "heading")))
        .order_by(Block.idx.desc(), Segment.idx.desc()).limit(2)).scalars().all()
    return " ".join(reversed(rows))[-700:]


def _save(session: Session, segment: Segment, text: str, *, model: str, call_id: int | None,
          glossary: GlossaryIndex) -> None:
    version = SegmentVersion(segment_id=segment.id, stage="translate", text=text, is_current=True,
                             model=model, llm_call_id=call_id)
    session.add(version)
    session.flush()
    findings = check_segment(segment.source_text, text, glossary)
    for f in findings:
        session.add(QAFlag(segment_id=segment.id, version_id=version.id, check_name=f.check,
                           severity=f.severity, message=f.message))
    segment.status = "flagged" if any(f.severity == "critical" for f in findings) else "translated"


def translate_chunk(session: Session, job: Job) -> None:
    chapter_id = uuid.UUID(job.payload["chapter_id"])
    ids = [uuid.UUID(i) for i in job.payload["segment_ids"]]
    rows = session.execute(
        select(Segment, Block.type, Block.idx).join(Block, Block.id == Segment.block_id)
        .where(Segment.id.in_(ids)).order_by(Block.idx, Segment.idx)).all()
    if not rows:
        return                                   # the document was re-parsed; a new plan supersedes this job
    done = set(session.scalars(
        select(SegmentVersion.segment_id).where(SegmentVersion.segment_id.in_(ids), SegmentVersion.is_current)))
    todo = [(seg, kind) for seg, kind, _ in rows if seg.id not in done]
    chapter = session.get(Chapter, chapter_id)
    glossary = load_index(session, job.project_id)

    # 1. translation memory: identical source text already translated
    memory = tm_lookup(session, job.project_id, [seg.source_text for seg, _ in todo])
    remaining = []
    for seg, kind in todo:
        if seg.source_text in memory:
            _save(session, seg, memory[seg.source_text], model="translation-memory", call_id=None, glossary=glossary)
        else:
            remaining.append((seg, kind))

    # 2. the model, for the rest
    if remaining:
        local = {f"s{n}": seg for n, (seg, _) in enumerate(remaining, start=1)}
        source_text = " ".join(seg.source_text for seg, _ in remaining)
        payload = {
            "chapter": chapter.title,
            "glossary": [{"en": t.source, "ka": t.target} for t in glossary.find(source_text)],
            "context_before": _context_before(session, chapter_id, rows[0][2]),
            "segments": [{"id": sid, "kind": KIND.get(kind, kind), "en": seg.source_text}
                         for sid, (seg, kind) in zip(local, remaining, strict=True)],
        }
        result = get_gateway(session).complete(
            "translate",
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            project_id=job.project_id, job_id=job.id,
        )
        answer = result.data.get("segments") if isinstance(result.data, dict) else None
        got = {item.get("id"): item.get("ka") for item in answer or [] if isinstance(item, dict)}
        missing = [sid for sid in local if not isinstance(got.get(sid), str) or not got[sid].strip()]
        if missing:
            # nothing is saved from a partial answer: the job retries and the chunk stays consistent
            raise LLMError(f"model answer is missing {len(missing)} of {len(local)} segments: {missing[:5]}")
        for sid, seg in local.items():
            text = got[sid].strip()
            _save(session, seg, text, model=result.model, call_id=result.call_id, glossary=glossary)
            if seg.status == "translated":       # only clean translations feed the memory
                tm_add(session, job.project_id, seg.source_text, text, "pipeline")

    session.flush()
    enqueue_advance(session, job)


def enqueue_advance(session: Session, job: Job) -> None:
    """Ask for the chapter's stage to be re-evaluated once this job has committed.

    Two chunk jobs finishing at the same moment cannot see each other's uncommitted work, so
    neither can tell that the stage is complete. A follow-up job, run afterwards, can.
    """
    jobs.enqueue(session, "advance_chapter", f"advance:{job.payload['chapter_id']}:{job.id}",
                 {"chapter_id": job.payload["chapter_id"]}, project_id=job.project_id, priority=5)
