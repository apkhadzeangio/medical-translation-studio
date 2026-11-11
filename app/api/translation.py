import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Block, Chapter, Document, LLMCall, Project, QAFlag, Segment, SegmentVersion
from app.pipeline.review import plan_qa, plan_review, qa_model
from app.pipeline.translate import plan_chapter

router = APIRouter(prefix="/api", tags=["translation"])


@router.post("/chapters/{chapter_id}/translate")
def translate_chapter(chapter_id: uuid.UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Queue a chapter for translation. Safe to call again: finished work is not redone."""
    if session.get(Chapter, chapter_id) is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    plan = plan_chapter(session, chapter_id)
    session.commit()
    return plan


@router.post("/documents/{document_id}/translate")
def translate_document(document_id: uuid.UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    if document.status != "parsed":
        raise HTTPException(status_code=409, detail=f"document is {document.status}, not parsed yet")
    plans = [plan_chapter(session, chapter_id) for chapter_id in session.scalars(
        select(Chapter.id).where(Chapter.document_id == document_id).order_by(Chapter.idx))]
    session.commit()
    return {"chapters": len(plans), "chunks": sum(p["chunks"] for p in plans), "words": sum(p["words"] for p in plans),
            "estimated_cost_usd": round(sum(p["estimated_cost_usd"] for p in plans), 2), "estimate_is_rough": True}


@router.post("/chapters/{chapter_id}/review")
def review_chapter(chapter_id: uuid.UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Queue the medical review of a chapter (normally queued automatically after translation)."""
    if session.get(Chapter, chapter_id) is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    plan = plan_review(session, chapter_id)
    session.commit()
    return plan


@router.post("/chapters/{chapter_id}/qa")
def qa_chapter(chapter_id: uuid.UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Queue the independent QA of a chapter. Needs LLM_QA_MODEL."""
    if session.get(Chapter, chapter_id) is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    if not qa_model():
        raise HTTPException(status_code=409, detail="no QA model configured (LLM_QA_MODEL)")
    plan = plan_qa(session, chapter_id)
    session.commit()
    return plan


@router.get("/segments/{segment_id}/versions")
def segment_versions(segment_id: uuid.UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Everything that happened to one segment: each stage's text, and every QA finding."""
    segment = session.get(Segment, segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="segment not found")
    versions = session.scalars(select(SegmentVersion).where(SegmentVersion.segment_id == segment_id)
                               .order_by(SegmentVersion.created_at, SegmentVersion.id))
    flags = session.scalars(select(QAFlag).where(QAFlag.segment_id == segment_id).order_by(QAFlag.created_at))
    return {
        "source": segment.source_text, "status": segment.status,
        "versions": [{"stage": v.stage, "text": v.text, "current": v.is_current, "model": v.model, "note": v.note,
                      "created_at": v.created_at.isoformat()} for v in versions],
        "flags": [{"check": f.check_name, "severity": f.severity, "message": f.message, "resolved": f.resolved}
                  for f in flags],
    }


@router.get("/chapters/{chapter_id}/segments")
def chapter_segments(chapter_id: uuid.UUID, limit: int = 200, offset: int = 0, flagged_only: bool = False,
                     session: Session = Depends(get_session)) -> dict[str, Any]:
    """Source and current translation side by side, in reading order, with open QA flags."""
    chapter = session.get(Chapter, chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    query = (select(Segment, Block.type, Block.page_no, SegmentVersion.text, SegmentVersion.model)
             .join(Block, Block.id == Segment.block_id)
             .outerjoin(SegmentVersion, (SegmentVersion.segment_id == Segment.id) & SegmentVersion.is_current)
             .where(Segment.chapter_id == chapter_id))
    if flagged_only:
        query = query.where(Segment.status == "flagged")
    rows = session.execute(query.order_by(Block.idx, Segment.idx).limit(min(max(limit, 1), 1000))
                           .offset(max(offset, 0))).all()
    flags: dict[uuid.UUID, list[dict]] = {}
    for flag in session.scalars(select(QAFlag).where(
            QAFlag.segment_id.in_([seg.id for seg, *_ in rows]), ~QAFlag.resolved)):
        flags.setdefault(flag.segment_id, []).append(
            {"check": flag.check_name, "severity": flag.severity, "message": flag.message})
    counts = dict(session.execute(
        select(Segment.status, func.count()).where(Segment.chapter_id == chapter_id).group_by(Segment.status)).all())
    return {
        "chapter": {"id": str(chapter.id), "title": chapter.title, "status": chapter.status},
        "counts": counts,
        "segments": [{"id": str(seg.id), "kind": kind, "page": page, "status": seg.status,
                      "translatable": seg.translatable, "source": seg.source_text, "translation": text,
                      "translated_by": model, "flags": flags.get(seg.id, [])}
                     for seg, kind, page, text, model in rows],
    }


@router.get("/projects/{project_id}/cost")
def project_cost(project_id: uuid.UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    rows = session.execute(
        select(LLMCall.stage, func.count(), func.sum(LLMCall.input_tokens), func.sum(LLMCall.output_tokens),
               func.sum(LLMCall.cost_usd)).where(LLMCall.project_id == project_id).group_by(LLMCall.stage)).all()
    stages = {stage: {"calls": calls, "input_tokens": int(tin or 0), "output_tokens": int(tout or 0),
                      "cost_usd": float(cost or 0)} for stage, calls, tin, tout, cost in rows}
    spent = sum((Decimal(str(s["cost_usd"])) for s in stages.values()), Decimal(0))
    return {"budget_usd": float(project.budget_usd) if project.budget_usd is not None else None,
            "spent_usd": float(spent), "stages": stages}
