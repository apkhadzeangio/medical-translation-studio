import secrets

from fastapi import Depends, FastAPI, Header, HTTPException

from app.api import health, projects
from app.config import get_settings


def require_token(authorization: str | None = Header(default=None)) -> None:
    """Single shared token until real auth lands in PR-12. Open if API_TOKEN is unset."""
    token = get_settings().api_token
    if not token:
        return
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(status_code=401, detail="invalid or missing token")


def create_app() -> FastAPI:
    app = FastAPI(title="Medical Translation Studio", version="0.1.0")
    app.include_router(health.router)
    app.include_router(projects.router, dependencies=[Depends(require_token)])
    return app


app = create_app()
