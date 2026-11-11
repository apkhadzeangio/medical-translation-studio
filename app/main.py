import secrets
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException

from app.api import documents, export, glossary, health, projects, translation
from app.config import get_settings


def require_token(authorization: str | None = Header(default=None)) -> None:
    """Single shared token until real auth lands in PR-12. Open if API_TOKEN is unset."""
    token = get_settings().api_token
    if not token:
        return
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(status_code=401, detail="invalid or missing token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker = None
    if get_settings().embedded_worker:
        from app.db import get_engine
        from app.worker import Worker, default_handlers
        worker = Worker(get_engine(), default_handlers())
        threading.Thread(target=worker.run_forever, name="mts-worker", daemon=True).start()
    yield
    if worker is not None:
        worker.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="Medical Translation Studio", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    for router in (projects.router, documents.router, glossary.router, translation.router, export.router):
        app.include_router(router, dependencies=[Depends(require_token)])
    return app


app = create_app()
