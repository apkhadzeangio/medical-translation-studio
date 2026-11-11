"""Medical review, independent QA, and moving a chapter from one stage to the next.

Review may change text, but only by proposing edits: an edit that would break a number, a
symbol or the markup is refused and the previous text stands. QA never changes text; it
only reports. Both are safe to run twice.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session

from app import jobs
from app.chunking import plan_chunks
from app.config import get_settings
from app.llm.gateway import LLMGateway
from app.models import Block, Chapter, Document, Job, Project, QAFlag, Segment, SegmentVersion
from app.pipeline import translate
from app.pipeline.checks import check_segment
from app.terminology import GlossaryIndex, load_index, tm_add

DETERMINISTIC = ("empty", "numbers", "markup", "symbols", "untranslated", "length", "glossary")
SEVERITIES = ("critical", "major", "minor")

REVIEW_SYSTEM = """You are a vascular surgeon reviewing a Georgian translation of an English medical \
textbook against its source.

Input is JSON: {"glossary": [{"en","ka"}], "segments": [{"id","kind","en","ka","issues":[...]}]}.
"issues" lists problems an automatic check already found in that segment; fix them.

Correct a segment ONLY if it has: a mistranslation; omitted or added clinical content; a wrong number,
dose, unit, inequality sign, side (left/right) or anatomical term; a lost or reversed negation or
qualifier; a glossary term not used; or Georgian that a Georgian physician would find wrong or unclear.
Do not rewrite a correct segment to suit your own style.

Every corrected text must keep all numbers, units, symbols, citation markers and <sup>/<sub> tags of
the source exactly.

Return ONLY JSON:
{"edits": [{"id": "...", "ka": "<the full corrected segment>", "severity": "critical|major|minor",
"reason": "<one short sentence in Georgian>"}]}
Return {"edits": []} if nothing needs correction."""

QA_SYSTEM = """You are an independent quality auditor for a Georgian translation of an English medical \
textbook. You did not produce this translation. You report problems; you do not rewrite.

Input is JSON: {"segments": [{"id","kind","en","ka"}]}.
Compare each Georgian segment with its English source and report every real problem:
mistranslation, omission, addition, number, unit, negation, laterality, terminology, grammar.
Ignore pure style preferences.

Return ONLY JSON:
{"findings": [{"id": "...", "category": "mistranslation|omission|addition|number|unit|negation|laterality|\
terminology|grammar", "severity": "critical|major|minor", "comment": "<one short sentence in Georgian>"}]}
critical = could mislead clinical understanding. Return {"findings": []} if the segments are sound."""


@lru_cache
def _qa_gateway(engine: Engine) -> LLMGateway:
    settings = get_settings()
    return LLMGateway(engine, settings.model_copy(update={
        "llm_base_url": settings.llm_qa_base_url or settings.llm_base_url,
        "llm_api_keys": settings.llm_qa_api_keys or settings.llm_api_keys,
    }))


def get_qa_gateway(session: Session) -> LLMGateway:
    settings = get_settings()
    if settings.llm_qa_base_url or settings.llm_qa_api_keys:
        return _qa_gateway(session.get_bind())
    return translate.get_gateway(session)


def qa_model() -> str | None:
    return get_settings().llm_qa_model or None


def qa_is_independent() -> bool:
    settings = get_settings()
    return bool(qa_model()) and qa_model() != (settings.llm_review_model or settings.llm_model)


# ----------------------------------------------------------------------------- shared helpers


def _plan(session: Session, chapter_id: uuid.UUID, kind: str) -> dict[str, Any]:
    chapter = session.get(Chapter, chapter_id)
    document = session.get(Document, chapter.document_id)
    chunks = plan_chunks(session, chapter_id, get_settings().translate_chunk_words)
    queued = 0
    for index, chunk in enumerate(chunks):
        digest = hashlib.sha256("".join(map(str, chunk.segment_ids)).encode()).hexdigest()[:16]
        job = jobs.enqueue(
            session, f"{kind}_chunk", f"{kind}:{chapter_id}:{digest}",
            {"chapter_id": str(chapter_id), "segment_ids": [str(i) for i in chunk.segment_ids]},
            project_id=document.project_id, priority=200 + index)
        queued += job.status == "queued"
    return {"chapter_id": str(chapter_id), "stage": kind, "chunks": len(chunks), "jobs_queued": queued}


def plan_review(session: Session, chapter_id: uuid.UUID) -> dict[str, Any]:
    return _plan(session, chapter_id, "review")


def plan_qa(session: Session, chapter_id: uuid.UUID) -> dict[str, Any]:
    return {**_plan(session, chapter_id, "qa"), "independent": qa_is_independent()}


def _load(session: Session, job: Job, done_column) -> list[tuple[Segment, str, SegmentVersion]]:
    """Segments of this chunk that still need this stage, with their current translation."""
    ids = [uuid.UUID(i) for i in job.payload["segment_ids"]]
    return list(session.execute(
        select(Segment, Block.type, SegmentVersion)
        .join(Block, Block.id == Segment.block_id)
        .join(SegmentVersion, (SegmentVersion.segment_id == Segment.id) & SegmentVersion.is_current)
        .where(Segment.id.in_(ids), ~done_column, Segment.status != "approved")
        .order_by(Block.idx, Segment.idx)).all())


def _open_flags(session: Session, segment_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[QAFlag]]:
    out: dict[uuid.UUID, list[QAFlag]] = {}
    for flag in session.scalars(select(QAFlag).where(QAFlag.segment_id.in_(segment_ids), ~QAFlag.resolved)):
        out.setdefault(flag.segment_id, []).append(flag)
    return out


def _settle(session: Session, segment: Segment, clean_status: str) -> None:
    critical = session.scalar(select(func.count()).select_from(QAFlag).where(
        QAFlag.segment_id == segment.id, ~QAFlag.resolved, QAFlag.severity == "critical"))
    segment.status = "flagged" if critical else clean_status


# ----------------------------------------------------------------------------- review


def _apply_edit(session: Session, segment: Segment, current: SegmentVersion, text: str, reason: str,
                glossary: GlossaryIndex, *, model: str, call_id: int) -> bool:
    """Replace the current translation with the reviewer's, unless that would damage it."""
    before = {f.check for f in check_segment(segment.source_text, current.text) if f.severity == "critical"}
    findings = check_segment(segment.source_text, text, glossary)
    broken = {f.check for f in findings if f.severity == "critical"} - before
    if broken:
        session.add(QAFlag(
            segment_id=segment.id, version_id=current.id, check_name="review_rejected", severity="minor",
            message=f"რევიზიის შესწორება უარყოფილია ({', '.join(sorted(broken))}): {reason}"[:1000]))
        return False
    current.is_current = False
    session.flush()
    version = SegmentVersion(segment_id=segment.id, stage="review", text=text, is_current=True, model=model,
                             llm_call_id=call_id, note=reason[:1000])
    session.add(version)
    session.flush()
    session.execute(update(QAFlag).where(
        QAFlag.segment_id == segment.id, ~QAFlag.resolved, QAFlag.check_name.in_(DETERMINISTIC),
    ).values(resolved=True))
    for f in findings:
        session.add(QAFlag(segment_id=segment.id, version_id=version.id, check_name=f.check,
                           severity=f.severity, message=f.message))
    return True


def review_chunk(session: Session, job: Job) -> None:
    rows = _load(session, job, Segment.review_done)
    if rows:
        settings = get_settings()
        glossary = load_index(session, job.project_id)
        flags = _open_flags(session, [seg.id for seg, _, _ in rows])
        local = {f"s{n}": row for n, row in enumerate(rows, start=1)}
        source_text = " ".join(seg.source_text for seg, _, _ in rows)
        payload = {
            "glossary": [{"en": t.source, "ka": t.target} for t in glossary.find(source_text)],
            "segments": [{"id": sid, "kind": translate.KIND.get(kind, kind), "en": seg.source_text,
                          "ka": version.text, "issues": [f.message for f in flags.get(seg.id, [])]}
                         for sid, (seg, kind, version) in local.items()],
        }
        result = translate.get_gateway(session).complete(
            "review",
            [{"role": "system", "content": REVIEW_SYSTEM},
             {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            model=settings.llm_review_model or settings.llm_model, project_id=job.project_id, job_id=job.id)
        edits = result.data.get("edits") if isinstance(result.data, dict) else None
        for edit in edits if isinstance(edits, list) else []:
            row = local.get(edit.get("id")) if isinstance(edit, dict) else None
            text = edit.get("ka") if row else None
            if not isinstance(text, str) or not text.strip() or text.strip() == row[2].text:
                continue
            seg, _, version = row
            applied = _apply_edit(session, seg, version, text.strip(), str(edit.get("reason") or ""), glossary,
                                  model=result.model, call_id=result.call_id)
            if applied:
                session.flush()
        for seg, _, _ in rows:
            seg.review_done = True
            session.flush()
            _settle(session, seg, "reviewed")
            if seg.status == "reviewed":           # the memory keeps the reviewed wording
                text = session.execute(select(SegmentVersion.text).where(
                    SegmentVersion.segment_id == seg.id, SegmentVersion.is_current)).scalar_one()
                tm_add(session, job.project_id, seg.source_text, text, "pipeline")
    translate.enqueue_advance(session, job)


# ----------------------------------------------------------------------------- QA


def qa_chunk(session: Session, job: Job) -> None:
    rows = _load(session, job, Segment.qa_done)
    if rows:
        local = {f"s{n}": row for n, row in enumerate(rows, start=1)}
        payload = {"segments": [{"id": sid, "kind": translate.KIND.get(kind, kind), "en": seg.source_text,
                                 "ka": version.text} for sid, (seg, kind, version) in local.items()]}
        result = get_qa_gateway(session).complete(
            "qa",
            [{"role": "system", "content": QA_SYSTEM},
             {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            model=qa_model(), project_id=job.project_id, job_id=job.id)
        findings = result.data.get("findings") if isinstance(result.data, dict) else None
        for finding in findings if isinstance(findings, list) else []:
            row = local.get(finding.get("id")) if isinstance(finding, dict) else None
            comment = str(finding.get("comment") or "").strip() if row else ""
            if not comment:
                continue
            seg, _, version = row
            category = re.sub(r"[^a-z_]", "", str(finding.get("category") or "other").lower())[:40] or "other"
            severity = finding.get("severity") if finding.get("severity") in SEVERITIES else "minor"
            session.add(QAFlag(segment_id=seg.id, version_id=version.id, check_name=f"qa:{category}",
                               severity=severity, message=comment[:1000]))
        for seg, _, _ in rows:
            seg.qa_done = True
            session.flush()
            _settle(session, seg, seg.status if seg.status != "flagged" else "reviewed")
    translate.enqueue_advance(session, job)


# ----------------------------------------------------------------------------- stage transitions


def advance_chapter(session: Session, job: Job) -> None:
    """Move the chapter forward if its current stage is complete. Runs after every chunk job."""
    chapter = session.get(Chapter, uuid.UUID(job.payload["chapter_id"]))
    if chapter is None:
        return
    project = session.get(Project, job.project_id)
    options = project.settings or {}

    def remaining(condition) -> int:
        return session.scalar(select(func.count()).select_from(Segment).where(
            Segment.chapter_id == chapter.id, Segment.translatable, condition))

    if chapter.status == "translating" and remaining(Segment.status == "pending") == 0:
        chapter.status = "reviewing"
        if options.get("auto_review", True):
            plan_review(session, chapter.id)
    if chapter.status == "reviewing" and remaining(~Segment.review_done & (Segment.status != "approved")) == 0:
        chapter.status = "qa"
        if qa_model() and options.get("auto_qa", True):
            plan_qa(session, chapter.id)
        else:                                   # no QA model configured: deterministic checks only
            session.execute(update(Segment).where(Segment.chapter_id == chapter.id).values(qa_done=True))
    if chapter.status == "qa" and remaining(~Segment.qa_done & (Segment.status != "approved")) == 0 \
            and remaining(Segment.status == "flagged") == 0:
        chapter.status = "done"
