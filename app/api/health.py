from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.models import Job

router = APIRouter(tags=["health"])


@lru_cache
def expected_revision() -> str | None:
    """The newest migration shipped with this code, or None if the scripts are not on disk."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    for root in (Path.cwd(), Path(__file__).resolve().parents[2]):
        if (root / "alembic.ini").exists() and (root / "migrations").is_dir():
            config = Config(str(root / "alembic.ini"))
            config.set_main_option("script_location", str(root / "migrations"))
            return ScriptDirectory.from_config(config).get_current_head()
    return None


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is up. Does not touch the database."""
    return {"status": "ok"}


@router.get("/health/db")
def health_db(session: Session = Depends(get_session)) -> dict[str, str]:
    """Readiness: database reachable and migrated to the revision this code needs."""
    try:
        revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"database unavailable: {type(exc).__name__}") from exc
    if revision is None:
        raise HTTPException(status_code=503, detail="database not migrated")
    expected = expected_revision()
    if expected is not None and revision != expected:
        raise HTTPException(status_code=503, detail=f"database at revision {revision}, code needs {expected}")
    return {"status": "ok", "revision": revision}


@router.get("/health/worker")
def health_worker(request: Request, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Is the embedded job worker running, and what is in the queue. No secrets, no job content."""
    settings = get_settings()
    workers = getattr(request.app.state, "workers", [])
    alive = sum(thread.is_alive() for _, thread in workers)
    errors = sorted({worker.last_loop_error for worker, _ in workers if worker.last_loop_error})
    try:
        queue = dict(session.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"job queue unavailable: {type(exc).__name__}") from exc
    body = {"embedded": settings.embedded_worker, "threads": len(workers), "alive": alive,
            "loop_errors": errors, "queue": queue, "llm_keys": len(settings.llm_key_list),
            "llm_model": settings.llm_model}
    if settings.embedded_worker and (alive == 0 or errors):
        raise HTTPException(status_code=503, detail={"status": "worker not running", **body})
    return {"status": "ok" if settings.embedded_worker else "external worker expected", **body}
