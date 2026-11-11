"""Postgres work queue.

- `enqueue` is idempotent on the job's key, so re-planning never duplicates work.
- `claim` uses FOR UPDATE SKIP LOCKED: many workers can poll without blocking each other.
- A failed job is retried with backoff until `max_attempts`; a job whose worker died
  (still "running" long after it was locked) is put back by `requeue_stale`.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Job


def _now() -> datetime:
    return datetime.now(UTC)


def enqueue(session: Session, kind: str, key: str, payload: dict[str, Any] | None = None, *,
            project_id: uuid.UUID | None = None, priority: int = 100, max_attempts: int = 5,
            run_after: datetime | None = None) -> Job:
    """Add a job unless one with this key exists; returns the job either way."""
    values = {"kind": kind, "idempotency_key": key, "payload": payload or {}, "project_id": project_id,
              "priority": priority, "max_attempts": max_attempts}
    if run_after is not None:
        values["run_after"] = run_after
    session.execute(insert(Job).values(**values).on_conflict_do_nothing(index_elements=["idempotency_key"]))
    return session.execute(select(Job).where(Job.idempotency_key == key)).scalar_one()


def claim(session: Session, worker: str, kinds: list[str] | None = None) -> Job | None:
    """Take the next runnable job and mark it running. Commits, so the claim is visible at once."""
    query = (select(Job).where(Job.status == "queued", Job.run_after <= func.now())
             .order_by(Job.priority, Job.run_after, Job.id).limit(1).with_for_update(skip_locked=True))
    if kinds:
        query = query.where(Job.kind.in_(kinds))
    job = session.execute(query).scalar_one_or_none()
    if job is not None:
        job.status, job.locked_by, job.locked_at = "running", worker, _now()
        job.attempts += 1
    session.commit()
    return job


def complete(session: Session, job_id: int) -> None:
    session.execute(update(Job).where(Job.id == job_id).values(
        status="done", finished_at=_now(), locked_by=None, locked_at=None, last_error=None))
    session.commit()


def fail(session: Session, job_id: int, error: str) -> str:
    """Retry later with exponential backoff, or give up after max_attempts. Returns the new status."""
    job = session.get(Job, job_id)
    job.last_error = error[:2000]
    job.locked_by = job.locked_at = None
    if job.attempts >= job.max_attempts:
        job.status, job.finished_at = "failed", _now()
    else:
        job.status = "queued"
        job.run_after = _now() + timedelta(seconds=min(3600, 30 * 2 ** (job.attempts - 1)))
    session.commit()
    return job.status


def defer(session: Session, job_id: int, seconds: float, reason: str) -> None:
    """Put a job back without spending an attempt (budget reached, provider paused)."""
    job = session.get(Job, job_id)
    job.status, job.locked_by, job.locked_at = "queued", None, None
    job.attempts = max(0, job.attempts - 1)
    job.run_after = _now() + timedelta(seconds=seconds)
    job.last_error = reason[:2000]
    session.commit()


def requeue_stale(session: Session, older_than_seconds: float) -> int:
    """Recover jobs whose worker disappeared mid-run."""
    cutoff = _now() - timedelta(seconds=older_than_seconds)
    result = session.execute(
        update(Job).where(Job.status == "running", Job.locked_at < cutoff)
        .values(status="queued", locked_by=None, locked_at=None, last_error="worker lost; requeued"))
    session.commit()
    return result.rowcount


def retry_failed(session: Session, project_id: uuid.UUID) -> int:
    result = session.execute(
        update(Job).where(Job.project_id == project_id, Job.status == "failed")
        .values(status="queued", attempts=0, run_after=func.now(), finished_at=None))
    session.commit()
    return result.rowcount


def summary(session: Session, project_id: uuid.UUID) -> dict[str, dict[str, int]]:
    rows = session.execute(
        select(Job.kind, Job.status, func.count()).where(Job.project_id == project_id).group_by(Job.kind, Job.status))
    out: dict[str, dict[str, int]] = {}
    for kind, status, count in rows:
        out.setdefault(kind, {})[status] = count
    return out
