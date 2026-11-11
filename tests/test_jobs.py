import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import jobs
from app.chunking import plan_chunks
from app.llm.gateway import BudgetExceeded
from app.main import create_app
from app.models import Block, Chapter, Document, Job, Project, Segment
from app.storage import LocalStorage, get_storage
from app.worker import Defer, Worker

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_vascular_chapter.pdf"


@pytest.fixture()
def clean_jobs(migrated_db):
    with Session(migrated_db) as s:
        s.execute(update(Job).where(Job.status.in_(("queued", "running"))).values(status="cancelled"))
        s.commit()
    return migrated_db


def key() -> str:
    return f"test:{uuid.uuid4()}"


def test_enqueue_is_idempotent(clean_jobs):
    with Session(clean_jobs) as s:
        k = key()
        first = jobs.enqueue(s, "noop", k, {"n": 1})
        second = jobs.enqueue(s, "noop", k, {"n": 2})
        s.commit()
        assert first.id == second.id and second.payload == {"n": 1}


def test_claim_order_and_skip_locked(clean_jobs):
    with Session(clean_jobs) as s:
        low = jobs.enqueue(s, "noop", key(), priority=200).id
        high = jobs.enqueue(s, "noop", key(), priority=1).id
        later = jobs.enqueue(s, "noop", key(), priority=0, run_after=datetime.now(UTC) + timedelta(hours=1)).id
        s.commit()
    with Session(clean_jobs) as a, Session(clean_jobs) as b:
        # worker A has locked the best row but not committed yet; worker B must not wait for it
        locked = a.execute(select(Job).where(Job.status == "queued").order_by(Job.priority, Job.id)
                           .limit(1).with_for_update(skip_locked=True)).scalar_one()
        assert locked.id == later or locked.id == high
        got = jobs.claim(b, "worker-b", ["noop"])
        assert got.id in (high, low) and got.id != locked.id
        a.rollback()
    with Session(clean_jobs) as s:
        assert s.get(Job, later).status == "queued"          # not yet due


def test_retry_with_backoff_then_fail(clean_jobs):
    with Session(clean_jobs) as s:
        job_id = jobs.enqueue(s, "boom", key(), max_attempts=2).id
        s.commit()

    def boom(session, job):
        session.add(Project(name="must be rolled back"))
        raise RuntimeError("no luck")

    worker = Worker(clean_jobs, {"boom": boom})
    assert worker.run_once() == "queued"
    with Session(clean_jobs) as s:
        job = s.get(Job, job_id)
        assert job.attempts == 1 and "no luck" in job.last_error and job.run_after > datetime.now(UTC)
        assert worker.run_once() is None                      # backing off
        job.run_after = datetime.now(UTC)
        s.commit()
    assert worker.run_once() == "failed"
    with Session(clean_jobs) as s:
        assert s.get(Job, job_id).status == "failed"
        assert s.scalar(select(func.count()).select_from(Project).where(Project.name == "must be rolled back")) == 0
        assert jobs.retry_failed(s, uuid.uuid4()) == 0


def test_defer_and_budget_pause_do_not_spend_attempts(clean_jobs):
    with Session(clean_jobs) as s:
        a = jobs.enqueue(s, "later", key()).id
        b = jobs.enqueue(s, "broke", key()).id
        s.commit()

    def later(session, job):
        raise Defer(120, "provider paused")

    def broke(session, job):
        raise BudgetExceeded("project budget $1 reached")

    worker = Worker(clean_jobs, {"later": later, "broke": broke})
    assert {worker.run_once(), worker.run_once()} == {"deferred"}
    with Session(clean_jobs) as s:
        for job_id, text in ((a, "provider paused"), (b, "budget")):
            job = s.get(Job, job_id)
            assert (job.status, job.attempts) == ("queued", 0) and text in job.last_error
            assert job.run_after > datetime.now(UTC) + timedelta(seconds=60)


def test_lost_worker_job_is_recovered(clean_jobs):
    with Session(clean_jobs) as s:
        job_id = jobs.enqueue(s, "noop", key()).id
        s.commit()
        assert jobs.claim(s, "dead-worker", ["noop"]).id == job_id
        assert jobs.requeue_stale(s, 1800) == 0               # still fresh
        s.execute(update(Job).where(Job.id == job_id).values(locked_at=datetime.now(UTC) - timedelta(hours=2)))
        s.commit()
        assert jobs.requeue_stale(s, 1800) == 1
    ran = []
    assert Worker(clean_jobs, {"noop": lambda session, job: ran.append(job.id)}).run_once() == "done"
    assert ran == [job_id]


def test_upload_parse_and_resume_after_crash(clean_jobs, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.worker import default_handlers

    storage = LocalStorage(tmp_path)
    monkeypatch.setattr("app.ingest.job.get_storage", lambda: storage)
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app) as client:
        project = client.post("/api/projects", json={"name": "upload"}).json()["id"]
        url = f"/api/projects/{project}/documents"
        assert client.post(url, files={"file": ("x.pdf", b"not a pdf", "application/pdf")}).status_code == 415
        data = FIXTURE.read_bytes()
        doc = client.post(url, files={"file": ("chapter.pdf", data, "application/pdf")})
        assert doc.status_code == 201 and doc.json()["status"] == "uploaded"
        doc_id = doc.json()["id"]
        assert client.post(url, files={"file": ("again.pdf", data, "application/pdf")}).json()["id"] == doc_id
        assert client.get(f"/api/projects/{project}/jobs").json() == {"ingest_document": {"queued": 1}}

        # a worker takes the job and dies before finishing
        with Session(clean_jobs) as s:
            lost = jobs.claim(s, "dead-worker", ["ingest_document"])
            s.execute(update(Job).where(Job.id == lost.id).values(locked_at=datetime.now(UTC) - timedelta(hours=2)))
            s.commit()
            assert jobs.requeue_stale(s, 1800) == 1

        worker = Worker(clean_jobs, default_handlers())
        assert worker.run_once() == "done"
        detail = client.get(f"/api/documents/{doc_id}").json()
        assert detail["status"] == "parsed" and detail["page_count"] == 7
        assert detail["chapters"][0]["title"].startswith("17 Infrainguinal Reconstruction")
        assert detail["parse"]["tables"] == 4 and detail["parse"]["scanned_pages"] == [7]
        blocks = detail["blocks"]

        # parsing again (job retried, or reparse requested) replaces instead of duplicating
        assert client.post(f"/api/documents/{doc_id}/reparse").json()["status"] == "uploaded"
        assert worker.run_once() == "done"
        again = client.get(f"/api/documents/{doc_id}").json()
        assert again["blocks"] == blocks and again["segments"] == detail["segments"]
        assert client.get(f"/api/projects/{project}/jobs").json() == {"ingest_document": {"done": 1}}
        assert len(list((tmp_path / "documents" / doc_id / "assets").iterdir())) == 6   # 4 tables + 2 figures

    with Session(clean_jobs) as s:
        chapter = s.scalars(select(Chapter).where(Chapter.document_id == uuid.UUID(doc_id))).first()
        chunks = plan_chunks(s, chapter.id, max_words=400)
        ids = [i for c in chunks for i in c.segment_ids]
        translatable = s.scalar(select(func.count()).select_from(Segment).where(
            Segment.chapter_id == chapter.id, Segment.translatable))
        assert len(ids) == len(set(ids)) == translatable       # every segment exactly once
        assert all(c.words <= 400 for c in chunks[:-1]) or max(c.words for c in chunks) < 700
        # a table's cells stay in one chunk
        table = s.scalars(select(Block).where(Block.document_id == uuid.UUID(doc_id), Block.type == "table")).first()
        cell_ids = set(s.scalars(select(Segment.id).where(Segment.block_id == table.id)))
        assert sum(bool(cell_ids & set(c.segment_ids)) for c in chunks) == 1
        # no chunk ends on a heading
        last = [s.get(Block, s.get(Segment, c.segment_ids[-1]).block_id).type for c in chunks[:-1]]
        assert "heading" not in last
        assert s.scalar(select(func.count()).select_from(Document).where(Document.id == uuid.UUID(doc_id))) == 1
