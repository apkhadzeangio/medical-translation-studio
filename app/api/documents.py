import hashlib
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import jobs
from app.config import get_settings
from app.db import get_session
from app.models import Block, Chapter, Document, Project, Segment
from app.storage import Storage, get_storage

router = APIRouter(prefix="/api", tags=["documents"])


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    project_id: uuid.UUID
    filename: str
    format: str
    status: str
    page_count: int | None
    created_at: datetime


class ChapterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    idx: int
    title: str
    page_start: int | None
    page_end: int | None
    status: str


class DocumentDetail(DocumentOut):
    blocks: int
    segments: int
    parse: dict[str, Any] | None
    chapters: list[ChapterOut]
    last_error: str | None


@router.post("/projects/{project_id}/documents", response_model=DocumentOut, status_code=201)
def upload_document(project_id: uuid.UUID, file: UploadFile = File(...), session: Session = Depends(get_session),
                    storage: Storage = Depends(get_storage)) -> Document:
    """Store a PDF and queue it for parsing. Uploading the same file again returns the first copy."""
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="project not found")
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"file larger than {get_settings().max_upload_mb} MB")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(status_code=415, detail="only PDF files are accepted")

    digest = hashlib.sha256(data).hexdigest()
    existing = session.execute(
        select(Document).where(Document.project_id == project_id, Document.sha256 == digest)).scalar_one_or_none()
    if existing is not None:
        return existing

    document = Document(id=uuid.uuid4(), project_id=project_id, filename=(file.filename or "document.pdf")[:500],
                        format="pdf", sha256=digest, storage_key="")
    document.storage_key = f"documents/{document.id}/source.pdf"
    storage.put(document.storage_key, data)
    session.add(document)
    session.flush()
    jobs.enqueue(session, "ingest_document", f"ingest:{document.id}", {"document_id": str(document.id)},
                 project_id=project_id, priority=10)
    session.commit()
    return document


@router.get("/projects/{project_id}/documents", response_model=list[DocumentOut])
def list_documents(project_id: uuid.UUID, session: Session = Depends(get_session)) -> list[Document]:
    return list(session.scalars(
        select(Document).where(Document.project_id == project_id).order_by(Document.created_at)))


@router.get("/documents/{document_id}", response_model=DocumentDetail)
def get_document(document_id: uuid.UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    blocks = session.scalar(select(func.count()).select_from(Block).where(Block.document_id == document_id))
    segments = session.scalar(
        select(func.count()).select_from(Segment).join(Block).where(Block.document_id == document_id))
    chapters = list(session.scalars(
        select(Chapter).where(Chapter.document_id == document_id).order_by(Chapter.idx)))
    job = session.execute(
        select(jobs.Job).where(jobs.Job.idempotency_key == f"ingest:{document_id}")).scalar_one_or_none()
    return {**DocumentOut.model_validate(document).model_dump(), "blocks": blocks, "segments": segments,
            "parse": (document.meta or {}).get("parse"), "chapters": chapters,
            "last_error": job.last_error if job else None}


@router.post("/documents/{document_id}/reparse", response_model=DocumentOut)
def reparse_document(document_id: uuid.UUID, session: Session = Depends(get_session)) -> Document:
    """Parse again (after a parser fix). Existing blocks, segments and translations are replaced."""
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    job = jobs.enqueue(session, "ingest_document", f"ingest:{document.id}", {"document_id": str(document.id)},
                       project_id=document.project_id, priority=10)
    if job.status in ("done", "failed", "cancelled"):
        job.status, job.attempts, job.finished_at, job.run_after = "queued", 0, None, func.now()
    document.status = "uploaded"
    session.commit()
    return document


@router.get("/projects/{project_id}/jobs")
def job_summary(project_id: uuid.UUID, session: Session = Depends(get_session)) -> dict[str, dict[str, int]]:
    return jobs.summary(session, project_id)


@router.post("/projects/{project_id}/jobs/retry-failed")
def retry_failed_jobs(project_id: uuid.UUID, session: Session = Depends(get_session)) -> dict[str, int]:
    return {"requeued": jobs.retry_failed(session, project_id)}
