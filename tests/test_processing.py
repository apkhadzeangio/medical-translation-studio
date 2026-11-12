import uuid
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import jobs
from app.models import Job, Project


def test_pause_resume_and_cancel_never_delete_finished_work(migrated_db, client):
    with Session(migrated_db) as session:
        session.execute(update(Job).where(Job.status.in_(("queued", "running"))).values(status="cancelled"))
        project = Project(name="controls")
        session.add(project)
        session.flush()
        first = jobs.enqueue(session, "noop", str(uuid.uuid4()), project_id=project.id)
        second = jobs.enqueue(session, "noop", str(uuid.uuid4()), project_id=project.id)
        session.commit()
        project_id, first_id, second_id = project.id, first.id, second.id
        assert jobs.claim(session, "already working", ["noop"]).id == first_id
    base = f"/api/projects/{project_id}/processing"
    paused = client.post(base + "/pause").json()
    assert paused["state"] == "paused" and paused["running"] == 1
    with Session(migrated_db) as session:
        assert jobs.claim(session, "cannot take next", ["noop"]) is None
        jobs.complete(session, first_id)  # the request that had already started can finish
    cancelled = client.post(base + "/cancel").json()
    assert cancelled["affected"] == 1 and cancelled["state"] == "cancelled"
    with Session(migrated_db) as session:
        assert session.get(Job, first_id).status == "done"
        assert session.get(Job, second_id).status == "cancelled"
        assert jobs.claim(session, "still stopped", ["noop"]) is None
    resumed = client.post(base + "/resume").json()
    assert resumed["affected"] == 1 and resumed["state"] == "running"
    with Session(migrated_db) as session:
        assert jobs.claim(session, "resumed", ["noop"]).id == second_id
        jobs.complete(session, second_id)
        assert session.get(Job, first_id).status == "done"


def test_other_projects_keep_running_and_resume_keeps_retry_backoff(migrated_db, client):
    with Session(migrated_db) as session:
        session.execute(update(Job).where(Job.status.in_(("queued", "running"))).values(status="cancelled"))
        paused, active = Project(name="paused"), Project(name="active")
        session.add_all([paused, active])
        session.flush()
        jobs.enqueue(session, "noop", str(uuid.uuid4()), project_id=paused.id)
        runnable = jobs.enqueue(session, "noop", str(uuid.uuid4()), project_id=active.id)
        session.commit()
        paused_id, runnable_id = paused.id, runnable.id
    assert client.post(f"/api/projects/{paused_id}/processing/pause").status_code == 200
    with Session(migrated_db) as session:
        assert jobs.claim(session, "other project", ["noop"]).id == runnable_id
        jobs.complete(session, runnable_id)
        stopped = session.scalar(select(Job).where(Job.project_id == paused_id))
        assert stopped.attempts == 0


def test_budget_update_and_unknown_control_actions(client):
    project = client.post("/api/projects", json={"name": "budget", "budget_usd": 1}).json()
    url = f"/api/projects/{project['id']}"
    assert Decimal(client.patch(url, json={"budget_usd": 0.5}).json()["budget_usd"]) == Decimal("0.5")
    assert client.patch(url, json={"budget_usd": -1}).status_code == 422
    assert client.patch(url, json={"name": " "}).status_code == 422
    assert client.post(url + "/processing/delete").status_code == 422
    assert client.get(f"/api/projects/{uuid.uuid4()}/processing").status_code == 404
