"""Project job controls. Running requests finish; pause/cancel prevent the next claim."""
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Job, Project

router = APIRouter(prefix="/api/projects", tags=["processing"])
CANCEL_REASON = "cancelled by operator"


@router.get("/{project_id}/processing")
def processing_state(project_id: uuid.UUID, session: Session = Depends(get_session)) -> dict:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return {"state": (project.settings or {}).get("processing_state", "running"),
            "running": session.scalar(select(func.count()).select_from(Job).where(
                Job.project_id == project_id, Job.status == "running"))}


@router.post("/{project_id}/processing/{action}")
def control_processing(project_id: uuid.UUID, action: Literal["pause", "resume", "cancel"],
                       session: Session = Depends(get_session)) -> dict:
    project = session.scalar(select(Project).where(Project.id == project_id).with_for_update())
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    state = {"pause": "paused", "resume": "running", "cancel": "cancelled"}[action]
    project.settings = {**(project.settings or {}), "processing_state": state}
    affected = 0
    if action == "cancel":
        affected = session.execute(update(Job).where(
            Job.project_id == project_id, Job.status == "queued").values(
                status="cancelled", last_error=CANCEL_REASON, finished_at=func.now())).rowcount
    elif action == "resume":
        affected = session.execute(update(Job).where(
            Job.project_id == project_id, Job.status == "cancelled", Job.last_error == CANCEL_REASON).values(
                status="queued", last_error=None, finished_at=None, run_after=func.now())).rowcount
        affected += session.execute(update(Job).where(
            Job.project_id == project_id, Job.status == "queued", Job.last_error.like("paused:%budget%")
        ).values(run_after=func.now(), last_error=None)).rowcount
    session.commit()
    return {**processing_state(project_id, session), "affected": affected,
            "in_flight_requests_finish": True}
