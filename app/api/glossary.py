import csv
import io
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import GlossaryTerm, Project, TMEntry
from app.terminology import read_pairs, tm_add, upsert_terms

router = APIRouter(prefix="/api", tags=["glossary"])
MAX_IMPORT_BYTES = 20 * 1024 * 1024


class TermIn(BaseModel):
    source_term: str = Field(min_length=1, max_length=300)
    target_term: str = Field(min_length=1, max_length=300)
    note: str | None = None


class TermOut(TermIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    approved: bool


def _project(session: Session, project_id: uuid.UUID) -> None:
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="project not found")


def _upload(file: UploadFile) -> bytes:
    data = file.file.read(MAX_IMPORT_BYTES + 1)
    if len(data) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="file larger than 20 MB")
    return data


@router.get("/projects/{project_id}/glossary", response_model=list[TermOut])
def list_terms(project_id: uuid.UUID, q: str | None = None, limit: int = 100, offset: int = 0,
               session: Session = Depends(get_session)) -> list[GlossaryTerm]:
    query = select(GlossaryTerm).where(GlossaryTerm.project_id == project_id)
    if q:
        like = f"%{q.lower()}%"
        query = query.where(or_(func.lower(GlossaryTerm.source_term).like(like), GlossaryTerm.target_term.like(like)))
    query = query.order_by(func.lower(GlossaryTerm.source_term)).limit(min(max(limit, 1), 1000)).offset(max(offset, 0))
    return list(session.scalars(query))


@router.post("/projects/{project_id}/glossary", response_model=TermOut)
def save_term(project_id: uuid.UUID, body: TermIn, session: Session = Depends(get_session)) -> GlossaryTerm:
    """Create a term, or change the translation of an existing one (matched case-insensitively)."""
    _project(session, project_id)
    upsert_terms(session, project_id, [(body.source_term.strip(), body.target_term.strip(), body.note)])
    session.commit()
    return session.execute(select(GlossaryTerm).where(
        GlossaryTerm.project_id == project_id,
        func.lower(GlossaryTerm.source_term) == body.source_term.strip().lower())).scalar_one()


@router.post("/projects/{project_id}/glossary/import")
def import_terms(project_id: uuid.UUID, file: UploadFile = File(...),
                 session: Session = Depends(get_session)) -> dict:
    """CSV or TSV with columns: English term, Georgian term, optional note."""
    _project(session, project_id)
    rows, problems = read_pairs(_upload(file))
    result = upsert_terms(session, project_id, rows)
    session.commit()
    return {**result, "skipped": len(problems), "problems": problems[:50]}


@router.get("/projects/{project_id}/glossary/export")
def export_terms(project_id: uuid.UUID, session: Session = Depends(get_session)) -> Response:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["en", "ka", "note"])
    for term in session.scalars(select(GlossaryTerm).where(GlossaryTerm.project_id == project_id)
                                .order_by(func.lower(GlossaryTerm.source_term))):
        writer.writerow([term.source_term, term.target_term, term.note or ""])
    return Response("﻿" + out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="glossary.csv"'})


@router.delete("/glossary/{term_id}", status_code=204)
def delete_term(term_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    term = session.get(GlossaryTerm, term_id)
    if term is None:
        raise HTTPException(status_code=404, detail="term not found")
    session.delete(term)
    session.commit()


@router.post("/projects/{project_id}/tm/import")
def import_tm(project_id: uuid.UUID, file: UploadFile = File(...), session: Session = Depends(get_session)) -> dict:
    """CSV or TSV of already translated sentence pairs: English source, Georgian target."""
    _project(session, project_id)
    rows, problems = read_pairs(_upload(file))
    stored = sum(tm_add(session, project_id, source, target, "import") for source, target, _ in rows)
    session.commit()
    return {"stored": stored, "kept_existing": len(rows) - stored, "skipped": len(problems),
            "problems": problems[:50]}


@router.get("/projects/{project_id}/tm")
def tm_stats(project_id: uuid.UUID, session: Session = Depends(get_session)) -> dict[str, int]:
    rows = session.execute(
        select(TMEntry.origin, func.count()).where(TMEntry.project_id == project_id).group_by(TMEntry.origin))
    return dict(rows.all())
