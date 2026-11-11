"""The `ingest_document` job: stored PDF -> chapters, blocks, segments, assets."""
from __future__ import annotations

import uuid

import pymupdf
from sqlalchemy.orm import Session

from app.config import get_settings
from app.ingest.pdf_layout import analyse
from app.ingest.store import store_parsed
from app.models import Document, Job
from app.storage import get_storage


def ingest_document(session: Session, job: Job) -> None:
    document = session.get(Document, uuid.UUID(job.payload["document_id"]))
    if document is None:
        return                                   # deleted since it was queued
    if document.format != "pdf":
        raise ValueError(f"unsupported format {document.format!r}")
    storage = get_storage()
    document.status = "parsing"
    session.flush()

    reorder = None
    settings = get_settings()
    if settings.llm_key_list and job.payload.get("use_structure_model", True):
        from app.ingest.pdf_structure import make_reorder
        from app.llm.gateway import LLMGateway
        reorder = make_reorder(LLMGateway(session.get_bind(), settings), document.project_id)

    with pymupdf.open(stream=storage.get(document.storage_key), filetype="pdf") as pdf:
        parsed = analyse(pdf, reorder)
        # replace=True makes a retry after a crash start clean instead of duplicating blocks
        store_parsed(session, document, parsed, pdf=pdf, storage=storage, replace=True)
