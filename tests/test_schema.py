import uuid

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Base, Block, Document, Job, Project, Segment, SegmentVersion


def test_models_match_migrations(migrated_db):
    with migrated_db.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"models and migrations have drifted: {diff}"


def _segment(session: Session) -> Segment:
    project = Project(name="p")
    session.add(project)
    session.flush()
    doc = Document(project_id=project.id, filename="a.pdf", format="pdf", storage_key="k",
                   sha256=uuid.uuid4().hex * 2)
    session.add(doc)
    session.flush()
    bbox = {"x0": 0, "y0": 0, "x1": 1, "y1": 1}
    block = Block(document_id=doc.id, idx=0, type="paragraph", page_no=1, bbox=bbox)
    session.add(block)
    session.flush()
    seg = Segment(block_id=block.id, source_text="Text", source_hash="h")
    session.add(seg)
    session.flush()
    return seg


def test_only_one_current_version_per_segment(migrated_db):
    with Session(migrated_db) as s:
        seg = _segment(s)
        s.add(SegmentVersion(segment_id=seg.id, stage="translate", text="a", is_current=True))
        s.add(SegmentVersion(segment_id=seg.id, stage="review", text="b", is_current=False))
        s.flush()
        s.add(SegmentVersion(segment_id=seg.id, stage="human", text="c", is_current=True))
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()


def test_check_constraints_and_idempotent_jobs(migrated_db):
    with Session(migrated_db) as s:
        p = Project(name="p")
        s.add(p)
        s.flush()
        s.add(Document(project_id=p.id, filename="a.epub", format="epub", storage_key="k", sha256="x" * 64))
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()
    with Session(migrated_db) as s:
        s.add(Job(kind="translate", idempotency_key="translate:chunk:1"))
        s.flush()
        s.add(Job(kind="translate", idempotency_key="translate:chunk:1"))
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()


def test_downgrade_and_upgrade_roundtrip(migrated_db, alembic_cfg):
    command.downgrade(alembic_cfg, "base")
    with migrated_db.connect() as conn:
        left = conn.execute(text(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name <> 'alembic_version'")).scalar_one()
    assert left == 0
    command.upgrade(alembic_cfg, "head")
