import uuid
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.models import Block, Chapter, Document, LLMCall, Project, QAFlag, Segment, SegmentVersion
from app.pipeline.review import plan_qa, plan_refine, plan_review, qa_model
from app.pipeline.translate import plan_chapter

router = APIRouter(prefix="/api", tags=["translation"])


@router.get("/runtime")
def runtime_config() -> dict:
    settings = get_settings()
    return {"provider": urlparse(settings.llm_base_url).hostname, "keys_configured": len(settings.llm_key_list),
            "models": {"translate": settings.llm_model,
                       "review": settings.llm_review_model or settings.llm_model,
                       "refine": settings.llm_refine_model, "qa": settings.llm_qa_model}}


def structure_errors(document: Document, chapter: Chapter | None = None) -> list[dict[str, Any]]:
    """Parse problems that mean the stored structure is damaged, for the whole document or one chapter."""
    issues = ((document.meta or {}).get("parse") or {}).get("issues") or []
    errors = [i for i in issues if i.get("severity") == "error"]
    if chapter is not None and chapter.page_start and chapter.page_end:
        errors = [i for i in errors if chapter.page_start <= i.get("page", 0) <= chapter.page_end]
    return errors


def _hold_back(errors: list[dict[str, Any]], force: bool) -> None:
    """Paid translation of text whose structure is known to be wrong needs an explicit decision."""
    if errors and not force:
        raise HTTPException(status_code=409, detail={
            "message": f"parsing found {len(errors)} structural problem(s); fix the parser and reparse, "
                       "or repeat the request with force=true to translate anyway",
            "issues": errors})


def _stage_ready(session: Session, chapter_id: uuid.UUID) -> Chapter:
    chapter = session.get(Chapter, chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    missing = session.scalar(select(Segment.id).where(
        Segment.chapter_id == chapter_id, Segment.translatable,
        ~select(SegmentVersion.id).where(
            SegmentVersion.segment_id == Segment.id, SegmentVersion.is_current).exists()).limit(1))
    if missing:
        raise HTTPException(status_code=409, detail="translate every segment before running an editing or QA stage")
    return chapter


@router.post("/chapters/{chapter_id}/translate")
def translate_chapter(chapter_id: uuid.UUID, force: bool = False,
                      session: Session = Depends(get_session)) -> dict[str, Any]:
    """Queue a chapter for translation. Safe to call again: finished work is not redone."""
    chapter = session.get(Chapter, chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    _hold_back(structure_errors(session.get(Document, chapter.document_id), chapter), force)
    plan = plan_chapter(session, chapter_id)
    session.commit()
    return plan


@router.post("/documents/{document_id}/translate")
def translate_document(document_id: uuid.UUID, force: bool = False,
                       session: Session = Depends(get_session)) -> dict[str, Any]:
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    if document.status != "parsed":
        raise HTTPException(status_code=409, detail=f"document is {document.status}, not parsed yet")
    _hold_back(structure_errors(document), force)
    plans = [plan_chapter(session, chapter_id) for chapter_id in session.scalars(
        select(Chapter.id).where(Chapter.document_id == document_id).order_by(Chapter.idx))]
    session.commit()
    return {"chapters": len(plans), "chunks": sum(p["chunks"] for p in plans), "words": sum(p["words"] for p in plans),
            "estimated_cost_usd": round(sum(p["estimated_cost_usd"] for p in plans), 2), "estimate_is_rough": True}


@router.post("/chapters/{chapter_id}/review")
def review_chapter(chapter_id: uuid.UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Queue the medical review of a chapter (normally queued automatically after translation)."""
    chapter = _stage_ready(session, chapter_id)
    plan = plan_review(session, chapter_id)
    if plan["jobs_queued"]:
        chapter.status = "reviewing"
    session.commit()
    return plan


@router.post("/chapters/{chapter_id}/qa")
def qa_chapter(chapter_id: uuid.UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Queue the independent QA of a chapter. Needs LLM_QA_MODEL."""
    chapter = _stage_ready(session, chapter_id)
    if not qa_model():
        raise HTTPException(status_code=409, detail="no QA model configured (LLM_QA_MODEL)")
    plan = plan_qa(session, chapter_id)
    if plan["jobs_queued"]:
        chapter.status = "qa"
    session.commit()
    return plan


@router.post("/chapters/{chapter_id}/refine")
def refine_chapter(chapter_id: uuid.UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
    chapter = _stage_ready(session, chapter_id)
    if not get_settings().llm_refine_model:
        raise HTTPException(status_code=409, detail="no academic editor configured (LLM_REFINE_MODEL)")
    plan = plan_refine(session, chapter_id)
    if plan["jobs_queued"]:
        chapter.status = "refining"
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
        "versions": [{"id": str(v.id), "stage": v.stage, "text": v.text, "current": v.is_current,
                      "model": v.model, "note": v.note, "created_by": v.created_by,
                      "prompt_version": session.get(LLMCall, v.llm_call_id).prompt_version if v.llm_call_id else None,
                      "created_at": v.created_at.isoformat()} for v in versions],
        "flags": [{"id": str(f.id), "check": f.check_name, "severity": f.severity,
                   "message": f.message, "resolved": f.resolved, "resolution_note": f.resolution_note,
                   "resolved_by": f.resolved_by}
                  for f in flags],
    }


@router.get("/chapters/{chapter_id}/segments")
def chapter_segments(chapter_id: uuid.UUID, limit: int = 200, offset: int = 0, flagged_only: bool = False,
                     session: Session = Depends(get_session)) -> dict[str, Any]:
    """Source and current translation side by side, in reading order, with open QA flags."""
    chapter = session.get(Chapter, chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    query = (select(Segment, Block.type, Block.page_no, SegmentVersion.text, SegmentVersion.model, SegmentVersion.id)
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
            {"id": str(flag.id), "check": flag.check_name, "severity": flag.severity, "message": flag.message})
    counts = dict(session.execute(
        select(Segment.status, func.count()).where(Segment.chapter_id == chapter_id, Segment.translatable)
        .group_by(Segment.status)).all())
    total_query = select(func.count()).select_from(Segment).where(Segment.chapter_id == chapter_id)
    if flagged_only:
        total_query = total_query.where(Segment.status == "flagged")
    total = session.scalar(total_query)
    return {
        "chapter": {"id": str(chapter.id), "title": chapter.title, "status": chapter.status},
        "counts": counts, "total": total, "offset": max(offset, 0),
        "segments": [{"id": str(seg.id), "kind": kind, "page": page, "status": seg.status,
                      "translatable": seg.translatable, "source": seg.source_text, "translation": text,
                      "translated_by": model, "version_id": str(version_id) if version_id else None,
                      "flags": flags.get(seg.id, [])}
                     for seg, kind, page, text, model, version_id in rows],
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
