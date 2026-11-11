from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_session

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is up. Does not touch the database."""
    return {"status": "ok"}


@router.get("/health/db")
def health_db(session: Session = Depends(get_session)) -> dict[str, str]:
    """Readiness: database reachable and migrated."""
    try:
        revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"database unavailable: {type(exc).__name__}") from exc
    if revision is None:
        raise HTTPException(status_code=503, detail="database not migrated")
    return {"status": "ok", "revision": revision}
