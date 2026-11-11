"""Job worker.

    python -m app.worker            # run until stopped

Each job runs in its own database transaction: a handler that raises leaves no partial
writes behind, and the job is retried. Handlers must be safe to run twice.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Callable

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app import jobs
from app.llm.gateway import BudgetExceeded
from app.models import Job

log = logging.getLogger("mts.worker")
Handler = Callable[[Session, Job], None]

STALE_AFTER_SECONDS = 30 * 60
BUDGET_PAUSE_SECONDS = 60 * 60


class Defer(Exception):
    """Raised by a handler to postpone its job without counting an attempt."""

    def __init__(self, seconds: float, reason: str) -> None:
        super().__init__(reason)
        self.seconds, self.reason = seconds, reason


class Worker:
    def __init__(self, engine: Engine, handlers: dict[str, Handler], *, name: str | None = None,
                 poll_seconds: float = 2.0) -> None:
        self.engine = engine
        self.handlers = handlers
        self.name = name or f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"
        self.poll_seconds = poll_seconds
        self.stop_event = threading.Event()

    def run_once(self) -> str | None:
        """Run one job. Returns its resulting status, or None if nothing was runnable."""
        with Session(self.engine) as session:
            job = jobs.claim(session, self.name, list(self.handlers))
            if job is None:
                return None
            job_id, kind = job.id, job.kind
        try:
            with Session(self.engine) as session:
                self.handlers[kind](session, session.get(Job, job_id))
                session.commit()
        except Defer as exc:
            with Session(self.engine) as session:
                jobs.defer(session, job_id, exc.seconds, exc.reason)
            return "deferred"
        except BudgetExceeded as exc:
            with Session(self.engine) as session:
                jobs.defer(session, job_id, BUDGET_PAUSE_SECONDS, f"paused: {exc}")
            return "deferred"
        except Exception as exc:  # noqa: BLE001 - any handler failure becomes a retry
            log.exception("job %s (%s) failed", job_id, kind)
            with Session(self.engine) as session:
                return jobs.fail(session, job_id, f"{type(exc).__name__}: {exc}")
        with Session(self.engine) as session:
            jobs.complete(session, job_id)
        return "done"

    def run_forever(self) -> None:
        last_sweep = 0.0
        while not self.stop_event.is_set():
            if time.monotonic() - last_sweep > 60:
                with Session(self.engine) as session:
                    recovered = jobs.requeue_stale(session, STALE_AFTER_SECONDS)
                if recovered:
                    log.warning("requeued %s job(s) left running by a lost worker", recovered)
                last_sweep = time.monotonic()
            if self.run_once() is None:
                self.stop_event.wait(self.poll_seconds)

    def stop(self) -> None:
        self.stop_event.set()


def default_handlers() -> dict[str, Handler]:
    from app.ingest.job import ingest_document

    return {"ingest_document": ingest_document}


def main() -> None:
    from app.db import get_engine

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker = Worker(get_engine(), default_handlers())
    signal.signal(signal.SIGTERM, lambda *_: worker.stop())
    signal.signal(signal.SIGINT, lambda *_: worker.stop())
    log.info("worker %s started", worker.name)
    worker.run_forever()


if __name__ == "__main__":
    main()
