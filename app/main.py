import logging
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.api import documents, editor, export, glossary, health, processing, projects, translation
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
    settings = get_settings()
    if settings.environment == "production" and not settings.api_token:
        # an open API on the internet could spend the whole model budget
        raise RuntimeError("API_TOKEN must be set when ENVIRONMENT=production")
    workers = []
    if settings.embedded_worker:
        from app.db import get_engine
        from app.worker import Worker, default_handlers
        if not logging.getLogger("mts").handlers:      # uvicorn configures only its own loggers
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
            logging.getLogger("mts").addHandler(handler)
            logging.getLogger("mts").setLevel(logging.INFO)
        for n in range(max(1, settings.worker_threads)):
            worker = Worker(get_engine(), default_handlers())
            thread = threading.Thread(target=worker.run_forever, name=f"mts-worker-{n}", daemon=True)
            thread.start()
            workers.append((worker, thread))
        logging.getLogger("mts.worker").info("started %s embedded worker thread(s)", len(workers))
    app.state.workers = workers
    yield
    for worker, _ in workers:
        worker.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="Medical Translation Studio", version="0.1.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web"), name="static")
    app.include_router(health.router)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def console() -> str:
        """The operator console: one static page that talks to /api with the access token."""
        return (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")

    for router in (projects.router, documents.router, glossary.router, translation.router,
                   editor.router, processing.router, export.router):
        app.include_router(router, dependencies=[Depends(require_token)])
    return app


app = create_app()
