"""Database schema.

PDF-first: every block and asset keeps its page number and bounding box so the
bilingual editor can show the source page region and export can re-place figures.
Text is never overwritten: each pipeline stage adds a SegmentVersion.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DOCUMENT_FORMATS = ("pdf", "docx")
DOCUMENT_STATUSES = ("uploaded", "parsing", "parsed", "failed")
CHAPTER_STATUSES = ("pending", "translating", "reviewing", "refining", "qa", "done", "failed")
BLOCK_TYPES = (
    "heading", "paragraph", "list_item", "list", "table", "figure", "caption", "footnote", "reference",
    "callout", "equation", "other",
)
ASSET_KINDS = ("figure", "table_image", "page_image")
SEGMENT_STATUSES = ("pending", "translated", "reviewed", "flagged", "approved")
VERSION_STAGES = ("translate", "review", "refine", "qa_fix", "human")
JOB_STATUSES = ("queued", "running", "done", "failed", "cancelled")
QA_SEVERITIES = ("critical", "major", "minor")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, datetime: DateTime(timezone=True)}


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    source_lang: Mapped[str] = mapped_column(String(8), server_default="en")
    target_lang: Mapped[str] = mapped_column(String(8), server_default="ka")
    budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    settings: Mapped[dict[str, Any]] = mapped_column(server_default=sa_text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _created()


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(_in("format", DOCUMENT_FORMATS), name="ck_documents_format"),
        CheckConstraint(_in("status", DOCUMENT_STATUSES), name="ck_documents_status"),
        UniqueConstraint("project_id", "sha256", name="uq_documents_project_sha256"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(500))
    format: Mapped[str] = mapped_column(String(8))
    storage_key: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64))
    page_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), server_default="uploaded")
    meta: Mapped[dict[str, Any]] = mapped_column(server_default=sa_text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _created()


class Chapter(Base):
    __tablename__ = "chapters"
    __table_args__ = (
        CheckConstraint(_in("status", CHAPTER_STATUSES), name="ck_chapters_status"),
        UniqueConstraint("document_id", "idx", name="uq_chapters_document_idx"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    idx: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500))
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), server_default="pending")


class Asset(Base):
    """Binary extracted from a source page: a figure, a rasterised table, or a page render."""

    __tablename__ = "assets"
    __table_args__ = (CheckConstraint(_in("kind", ASSET_KINDS), name="ck_assets_kind"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    page_no: Mapped[int | None] = mapped_column(Integer)
    bbox: Mapped[dict[str, Any] | None] = mapped_column()
    storage_key: Mapped[str] = mapped_column(String(500))
    mime: Mapped[str] = mapped_column(String(64))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)


class Block(Base):
    """Structural unit of the source in reading order."""

    __tablename__ = "blocks"
    __table_args__ = (
        CheckConstraint(_in("type", BLOCK_TYPES), name="ck_blocks_type"),
        UniqueConstraint("document_id", "idx", name="uq_blocks_document_idx"),
        Index("ix_blocks_chapter_idx", "chapter_id", "idx"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    chapter_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("chapters.id", ondelete="SET NULL"))
    idx: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(16))
    level: Mapped[int | None] = mapped_column(Integer)
    page_no: Mapped[int | None] = mapped_column(Integer)
    bbox: Mapped[dict[str, Any] | None] = mapped_column()
    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"))
    data: Mapped[dict[str, Any]] = mapped_column(server_default=sa_text("'{}'::jsonb"))


class Segment(Base):
    """Translatable unit: a paragraph, heading, caption, or one table cell."""

    __tablename__ = "segments"
    __table_args__ = (
        CheckConstraint(_in("status", SEGMENT_STATUSES), name="ck_segments_status"),
        UniqueConstraint("block_id", "idx", name="uq_segments_block_idx"),
        Index("ix_segments_chapter_status", "chapter_id", "status"),
        Index("ix_segments_source_hash", "source_hash"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    block_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("blocks.id", ondelete="CASCADE"))
    chapter_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("chapters.id", ondelete="SET NULL"))
    idx: Mapped[int] = mapped_column(Integer, server_default="0")
    source_text: Mapped[str] = mapped_column(Text)
    source_hash: Mapped[str] = mapped_column(String(64))
    translatable: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("true"))
    review_done: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"))
    refine_done: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"))
    qa_done: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"))
    status: Mapped[str] = mapped_column(String(16), server_default="pending")


class LLMCall(Base):
    """Cost and quota ledger: one row per provider request."""

    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_project_created", "project_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    stage: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    key_label: Mapped[str | None] = mapped_column(String(64))  # never the key itself
    input_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cached_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default="0")
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("true"))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class SegmentVersion(Base):
    __tablename__ = "segment_versions"
    __table_args__ = (
        CheckConstraint(_in("stage", VERSION_STAGES), name="ck_segment_versions_stage"),
        Index(
            "uq_segment_versions_current", "segment_id", unique=True, postgresql_where=sa_text("is_current")
        ),
        Index("ix_segment_versions_segment", "segment_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    segment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("segments.id", ondelete="CASCADE"))
    stage: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"))
    model: Mapped[str | None] = mapped_column(String(100))
    llm_call_id: Mapped[int | None] = mapped_column(ForeignKey("llm_calls.id", ondelete="SET NULL"))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = _created()


class QAFlag(Base):
    __tablename__ = "qa_flags"
    __table_args__ = (
        CheckConstraint(_in("severity", QA_SEVERITIES), name="ck_qa_flags_severity"),
        Index("ix_qa_flags_segment_open", "segment_id", postgresql_where=sa_text("NOT resolved")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    segment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("segments.id", ondelete="CASCADE"))
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("segment_versions.id", ondelete="CASCADE")
    )
    check_name: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[str | None] = mapped_column(String(100))
    resolved_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = _created()


class GlossaryTerm(Base):
    __tablename__ = "glossary_terms"
    __table_args__ = (
        Index("uq_glossary_project_term", "project_id", sa_text("lower(source_term)"), unique=True),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    source_term: Mapped[str] = mapped_column(String(300))
    target_term: Mapped[str] = mapped_column(String(300))
    note: Mapped[str | None] = mapped_column(Text)
    approved: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("true"))
    created_at: Mapped[datetime] = _created()


class TMEntry(Base):
    """Translation memory: approved source/target pairs. Embeddings are added in PR-7."""

    __tablename__ = "tm_entries"
    __table_args__ = (Index("uq_tm_project_hash", "project_id", "source_hash", unique=True),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    source_text: Mapped[str] = mapped_column(Text)
    source_hash: Mapped[str] = mapped_column(String(64))
    target_text: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(32), server_default="pipeline")
    created_at: Mapped[datetime] = _created()


class Job(Base):
    """Postgres-backed work queue. Workers claim rows with FOR UPDATE SKIP LOCKED (PR-6)."""

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(_in("status", JOB_STATUSES), name="ck_jobs_status"),
        UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),
        Index("ix_jobs_claim", "priority", "run_after", "id", postgresql_where=sa_text("status = 'queued'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=sa_text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(16), server_default="queued")
    priority: Mapped[int] = mapped_column(Integer, server_default="100")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="5")
    run_after: Mapped[datetime] = mapped_column(server_default=func.now())
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_at: Mapped[datetime | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column()
