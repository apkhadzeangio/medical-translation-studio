import re
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.export.docx import export_docx
from app.export.pdf import PdfUnavailable, docx_to_pdf
from app.models import Chapter, Document
from app.storage import Storage, get_storage

router = APIRouter(prefix="/api", tags=["export"])
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _respond(session: Session, storage: Storage, chapter_ids: list[uuid.UUID], name: str, fmt: str,
             mode: str) -> Response:
    if fmt not in ("docx", "pdf") or mode not in ("ka", "bilingual"):
        raise HTTPException(status_code=422, detail="format must be docx or pdf; mode must be ka or bilingual")
    data = export_docx(session, chapter_ids, mode=mode, storage=storage)
    media = DOCX_MIME
    if fmt == "pdf":
        try:
            data, media = docx_to_pdf(data), "application/pdf"
        except PdfUnavailable as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
    stem = re.sub(r"[^\w\-]+", "_", name, flags=re.ASCII).strip("_")[:80] or "export"
    filename = f"{stem}_{mode}.{fmt}"
    return Response(data, media_type=media, headers={
        "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"})


@router.get("/chapters/{chapter_id}/export")
def export_chapter(chapter_id: uuid.UUID, format: str = "docx", mode: str = "ka",
                   session: Session = Depends(get_session), storage: Storage = Depends(get_storage)) -> Response:
    """One chapter as DOCX or PDF. mode=ka is the Georgian text; mode=bilingual is a review table."""
    chapter = session.get(Chapter, chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="chapter not found")
    return _respond(session, storage, [chapter_id], f"chapter_{chapter.idx + 1}", format, mode)


@router.get("/documents/{document_id}/export")
def export_document(document_id: uuid.UUID, format: str = "docx", mode: str = "ka",
                    session: Session = Depends(get_session), storage: Storage = Depends(get_storage)) -> Response:
    """Every chapter of a document in one file."""
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    chapter_ids = list(session.scalars(select(Chapter.id).where(Chapter.document_id == document_id)))
    if not chapter_ids:
        raise HTTPException(status_code=409, detail="document has no chapters yet")
    return _respond(session, storage, chapter_ids, document.filename.rsplit(".", 1)[0], format, mode)
