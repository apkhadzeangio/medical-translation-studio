"""Versioned human corrections, QA decisions and approval.

Every write compares the version the editor actually saw. Segment row locks are also
used by machine stages, so a late worker cannot overwrite an operator's correction.
"""
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app import jobs
from app.db import get_session
from app.models import Chapter, Document, QAFlag, Segment, SegmentVersion, TMEntry
from app.pipeline.checks import check_segment
from app.pipeline.review import plan_qa, qa_model
from app.terminology import load_index, tm_add, tm_hash

router = APIRouter(prefix="/api", tags=["editor"])


class RevisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version_id: uuid.UUID | None
    actor: str = Field(default="operator", min_length=1, max_length=100)
    note: str = Field(default="", max_length=2000)


class EditIn(RevisionIn):
    text: str = Field(min_length=1, max_length=100000)

    @field_validator("text")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("translation must not be blank")
        return value.strip()


class ResolveIn(RevisionIn):
    note: str = Field(min_length=3, max_length=2000)

    @field_validator("note")
    @classmethod
    def explanation(cls, value: str) -> str:
        if len(value.strip()) < 3:
            raise ValueError("a reason is required")
        return value.strip()


def _lock(session: Session, segment_id: uuid.UUID, expected: uuid.UUID | None):
    segment = session.scalar(select(Segment).where(Segment.id == segment_id).with_for_update())
    if segment is None:
        raise HTTPException(status_code=404, detail="segment not found")
    if not segment.translatable:
        raise HTTPException(status_code=409, detail="this source block is preserved unchanged")
    current = session.scalar(select(SegmentVersion).where(
        SegmentVersion.segment_id == segment_id, SegmentVersion.is_current))
    if (current.id if current else None) != expected:
        raise HTTPException(status_code=409, detail={
            "message": "translation changed; reload it before saving",
            "current_version_id": str(current.id) if current else None})
    chapter = session.get(Chapter, segment.chapter_id)
    if chapter is None:
        raise HTTPException(status_code=409, detail="segment has no chapter")
    document = session.get(Document, chapter.document_id)
    return segment, current, chapter, document.project_id


def _advance(session: Session, chapter: Chapter, project_id: uuid.UUID, key: str) -> None:
    jobs.enqueue(session, "advance_chapter", f"human:{chapter.id}:{key}",
                 {"chapter_id": str(chapter.id)}, project_id=project_id, priority=5)


def _out(segment: Segment, version: SegmentVersion) -> dict[str, Any]:
    return {"id": str(segment.id), "version_id": str(version.id), "translation": version.text,
            "status": segment.status, "qa_done": segment.qa_done}


@router.put("/segments/{segment_id}/translation")
def save_translation(segment_id: uuid.UUID, body: EditIn,
                     session: Session = Depends(get_session)) -> dict[str, Any]:
    segment, current, chapter, project_id = _lock(session, segment_id, body.expected_version_id)
    if current and current.text == body.text:
        return _out(segment, current)
    if current:
        current.is_current = False
        # An edited approved pair must stop supplying the previous wording to other jobs.
        session.execute(delete(TMEntry).where(
            TMEntry.project_id == project_id, TMEntry.source_hash == tm_hash(segment.source_text),
            TMEntry.target_text == current.text))
        session.flush()
    version = SegmentVersion(segment_id=segment.id, stage="human", text=body.text, is_current=True,
                             created_by=body.actor, note=body.note or "human correction")
    session.add(version)
    session.flush()
    session.execute(update(QAFlag).where(QAFlag.segment_id == segment.id, ~QAFlag.resolved).values(
        resolved=True, resolved_by=body.actor, resolved_at=datetime.now(UTC),
        resolution_note="superseded by a new human revision; QA invalidated"))
    findings = check_segment(segment.source_text, body.text, load_index(session, project_id))
    for finding in findings:
        session.add(QAFlag(segment_id=segment.id, version_id=version.id, check_name=finding.check,
                           severity=finding.severity, message=finding.message))
    segment.status = "flagged" if any(f.severity == "critical" for f in findings) else "reviewed"
    # A human correction supersedes machine editing; the auditor must check the new version.
    segment.review_done = segment.refine_done = True
    segment.qa_done = not bool(qa_model())
    if chapter.status in ("pending", "done", "qa"):
        chapter.status = "qa"
    if qa_model():
        plan_qa(session, chapter.id)
    _advance(session, chapter, project_id, str(version.id))
    session.commit()
    return _out(segment, version)


@router.post("/segments/{segment_id}/approve")
def approve_translation(segment_id: uuid.UUID, body: RevisionIn,
                        session: Session = Depends(get_session)) -> dict[str, Any]:
    segment, current, chapter, project_id = _lock(session, segment_id, body.expected_version_id)
    if current is None:
        raise HTTPException(status_code=409, detail="no translation to approve")
    flags = list(session.scalars(select(QAFlag).where(QAFlag.segment_id == segment.id, ~QAFlag.resolved)))
    if any(flag.severity == "critical" for flag in flags):
        raise HTTPException(status_code=409, detail="correct or explicitly resolve critical QA findings first")
    if segment.status == "approved":
        return _out(segment, current)
    current.is_current = False
    session.flush()
    version = SegmentVersion(segment_id=segment.id, stage="human", text=current.text, is_current=True,
                             created_by=body.actor, note=body.note or "human approval")
    session.add(version)
    session.flush()
    for flag in flags:  # unchanged text: these minor/major findings remain relevant
        flag.version_id = version.id
    segment.status = "approved"
    segment.review_done = segment.refine_done = segment.qa_done = True
    tm_add(session, project_id, segment.source_text, version.text, "human")
    _advance(session, chapter, project_id, str(version.id))
    session.commit()
    return _out(segment, version)


@router.post("/qa-flags/{flag_id}/resolve")
def resolve_finding(flag_id: uuid.UUID, body: ResolveIn,
                    session: Session = Depends(get_session)) -> dict[str, Any]:
    flag = session.get(QAFlag, flag_id)
    if flag is None:
        raise HTTPException(status_code=404, detail="QA finding not found")
    segment, current, chapter, project_id = _lock(session, flag.segment_id, body.expected_version_id)
    session.refresh(flag)  # an editor could have superseded it while this request waited for the lock
    if not flag.resolved:
        flag.resolved, flag.resolved_by = True, body.actor
        flag.resolved_at, flag.resolution_note = datetime.now(UTC), body.note
        session.flush()
        critical = session.scalar(select(QAFlag.id).where(
            QAFlag.segment_id == segment.id, ~QAFlag.resolved, QAFlag.severity == "critical").limit(1))
        if critical is None and segment.status == "flagged":
            segment.status = "reviewed"
        _advance(session, chapter, project_id, f"resolve:{flag.id}")
    session.commit()
    return {"id": str(flag.id), "resolved": flag.resolved, "note": flag.resolution_note}
