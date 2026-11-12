import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from app.models import Block, Chapter, Document, Job, Project, QAFlag, Segment, SegmentVersion, TMEntry
from app.terminology import tm_add


@pytest.fixture()
def draft(migrated_db):
    with Session(migrated_db) as session:
        session.execute(update(Job).where(Job.status.in_(("queued", "running"))).values(status="cancelled"))
        project = Project(name="human editing")
        session.add(project)
        session.flush()
        document = Document(project_id=project.id, filename="chapter.pdf", format="pdf", status="parsed",
                            storage_key="test", sha256=uuid.uuid4().hex * 2)
        session.add(document)
        session.flush()
        chapter = Chapter(document_id=document.id, idx=0, title="Dose", status="done")
        session.add(chapter)
        session.flush()
        block = Block(document_id=document.id, chapter_id=chapter.id, idx=0, type="paragraph", page_no=1)
        session.add(block)
        session.flush()
        segment = Segment(block_id=block.id, chapter_id=chapter.id, source_text="The dose is 5 mg.",
                          source_hash="dose", status="reviewed", review_done=True, qa_done=True)
        session.add(segment)
        session.flush()
        version = SegmentVersion(segment_id=segment.id, stage="review", text="დოზაა 5 mg.", is_current=True)
        session.add(version)
        session.commit()
        return {"engine": migrated_db, "project": str(project.id), "document": str(document.id),
                "chapter": str(chapter.id), "segment": str(segment.id), "version": str(version.id)}


def test_human_edit_is_versioned_and_stale_save_keeps_the_newer_text(draft, client):
    url = f"/api/segments/{draft['segment']}/translation"
    body = {"expected_version_id": draft["version"], "text": "საჭირო დოზაა 5 mg.", "note": "clarity"}
    saved = client.put(url, json=body)
    assert saved.status_code == 200
    assert saved.json()["version_id"] != draft["version"]
    stale = client.put(url, json={**body, "text": "სხვა ტექსტი 5 mg."})
    assert stale.status_code == 409
    history = client.get(f"/api/segments/{draft['segment']}/versions").json()
    assert [(v["stage"], v["current"]) for v in history["versions"]] == [("review", False), ("human", True)]
    assert history["versions"][-1]["text"] == body["text"]
    assert history["versions"][-1]["created_by"] == "operator"


def test_simultaneous_editors_do_not_overwrite_each_other(draft, client):
    def save(text):
        return client.put(f"/api/segments/{draft['segment']}/translation", json={
            "expected_version_id": draft["version"], "text": text}).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, ["დოზაა 5 mg. დღეში", "დოზა არის 5 mg."]))
    assert sorted(results) == [200, 409]


def test_auditor_waiting_for_a_human_edit_reads_the_new_version(draft):
    from app.pipeline.review import _load

    engine, attempted_lock = draft["engine"], threading.Event()
    segment_id = uuid.UUID(draft["segment"])
    with Session(engine) as session:
        session.get(Segment, segment_id).qa_done = False
        session.commit()

    def before_query(connection, cursor, statement, parameters, context, executemany):
        if threading.current_thread().name.startswith("qa_reader") and "FOR UPDATE" in statement:
            attempted_lock.set()

    def audit():
        with Session(engine) as session:
            job = Job(payload={"segment_ids": [str(segment_id)]})
            return _load(session, job, Segment.qa_done)[0][2].text

    event.listen(engine, "before_cursor_execute", before_query)
    try:
        with Session(engine) as editor, ThreadPoolExecutor(max_workers=1, thread_name_prefix="qa_reader") as pool:
            editor.scalar(select(Segment).where(Segment.id == segment_id).with_for_update())
            old = editor.get(SegmentVersion, uuid.UUID(draft["version"]))
            old.is_current = False
            editor.flush()
            editor.add(SegmentVersion(segment_id=segment_id, stage="human", text="ახალი დოზაა 5 mg.",
                                      is_current=True))
            editor.flush()
            future = pool.submit(audit)
            started = attempted_lock.wait(5)
            editor.commit()
            assert started
            assert future.result(timeout=5) == "ახალი დოზაა 5 mg."
    finally:
        event.remove(engine, "before_cursor_execute", before_query)


def test_edit_invalidates_old_qa_and_requeues_the_changed_snapshot(draft, client, monkeypatch):
    from app.pipeline.review import plan_qa

    monkeypatch.setattr("app.api.editor.qa_model", lambda: "test-qa")
    with Session(draft["engine"]) as session:
        session.add(QAFlag(segment_id=uuid.UUID(draft["segment"]), version_id=uuid.UUID(draft["version"]),
                           check_name="qa:grammar", severity="major", message="old finding"))
        session.get(Segment, uuid.UUID(draft["segment"])).qa_done = False
        plan_qa(session, uuid.UUID(draft["chapter"]))
        old_job = session.scalar(select(Job).where(Job.project_id == uuid.UUID(draft["project"])))
        old_job.status = "done"
        session.get(Segment, uuid.UUID(draft["segment"])).qa_done = True
        tm_add(session, uuid.UUID(draft["project"]), "The dose is 5 mg.", "დოზაა 5 mg.", "human")
        session.commit()
    result = client.put(f"/api/segments/{draft['segment']}/translation", json={
        "expected_version_id": draft["version"], "text": "საჭირო დოზაა 5 mg."})
    assert result.status_code == 200 and result.json()["qa_done"] is False
    with Session(draft["engine"]) as session:
        flags = list(session.scalars(select(QAFlag).where(QAFlag.segment_id == uuid.UUID(draft["segment"]))))
        assert len(flags) == 1 and flags[0].resolved and "invalidated" in flags[0].resolution_note
        jobs = list(session.scalars(select(Job).where(Job.project_id == uuid.UUID(draft["project"]),
                                                      Job.kind == "qa_chunk")))
        assert sorted(j.status for j in jobs) == ["done", "queued"]
        assert session.scalar(select(TMEntry).where(TMEntry.project_id == uuid.UUID(draft["project"]))) is None


def test_critical_finding_needs_a_recorded_human_decision_before_approval(draft, client):
    saved = client.put(f"/api/segments/{draft['segment']}/translation", json={
        "expected_version_id": draft["version"], "text": "დოზაა 6 mg."}).json()
    body = {"expected_version_id": saved["version_id"]}
    approve = f"/api/segments/{draft['segment']}/approve"
    assert client.post(approve, json=body).status_code == 409
    history = client.get(f"/api/segments/{draft['segment']}/versions").json()
    flag = next(f for f in history["flags"] if not f["resolved"] and f["check"] == "numbers")
    resolve = f"/api/qa-flags/{flag['id']}/resolve"
    assert client.post(resolve, json={**body, "note": " "}).status_code == 422
    assert client.post(resolve, json={**body, "note": "Explicit operator decision for this test"}).status_code == 200
    approved = client.post(approve, json=body)
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    history = client.get(f"/api/segments/{draft['segment']}/versions").json()
    assert history["flags"][0]["resolved_by"] == "operator"
    with Session(draft["engine"]) as session:
        memory = session.scalar(select(TMEntry).where(TMEntry.project_id == uuid.UUID(draft["project"])))
        assert memory.origin == "human" and memory.target_text == "დოზაა 6 mg."


def test_final_export_requires_approval_but_a_draft_can_be_downloaded(draft, client):
    url = f"/api/chapters/{draft['chapter']}/export"
    assert client.get(url).status_code == 200
    response = client.get(url + "?final=true")
    assert response.status_code == 409 and response.json()["detail"]["quality"]["approved"] == 0
    assert client.post(f"/api/segments/{draft['segment']}/approve", json={
        "expected_version_id": draft["version"]}).status_code == 200
    response = client.get(url + "?final=true")
    assert response.status_code == 200 and response.content.startswith(b"PK")
    report = client.get(f"/api/documents/{draft['document']}/quality").json()
    assert report["ready_for_final_export"] is True and report["approved"] == 1


def test_missing_ids_and_blank_or_unversioned_edits_are_rejected(draft, client):
    url = f"/api/segments/{draft['segment']}/translation"
    assert client.put(url, json={"text": "hello"}).status_code == 422
    assert client.put(url, json={"text": " ", "expected_version_id": draft["version"]}).status_code == 422
    assert client.put(f"/api/segments/{uuid.uuid4()}/translation", json={
        "text": "hello", "expected_version_id": None}).status_code == 404


def test_runtime_does_not_disclose_a_key(client):
    response = client.get("/api/runtime")
    assert response.status_code == 200 and "api_keys" not in response.text
    assert set(response.json()["models"]) == {"translate", "review", "refine", "qa"}


def test_editor_script_is_packaged_and_served(client):
    assert client.get("/static/editor.js").status_code == 200
    assert "expected_version_id" in client.get("/static/editor.js").text
