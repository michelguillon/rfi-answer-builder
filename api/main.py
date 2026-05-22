"""api.main — FastAPI app entry point.

Run via:
    docker compose up backend

Which executes:
    uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.routers import answer, corpus, ingest, sessions
from api.session import cleanup_old_sessions

logger = logging.getLogger("api.main")
logging.basicConfig(level=logging.INFO)


# ARCHITECTURAL DECISION: lifespan context manager, not @app.on_event.
#
# FastAPI 0.93+ deprecated @app.on_event("startup"/"shutdown") in
# favour of a single asynccontextmanager that wraps the application
# lifetime. The lifespan form keeps startup and shutdown in one
# place, can hold state in the contextmanager's local scope, and is
# what FastAPI will still support five versions from now. The
# SPEC_UI snippet showed the older @app.on_event form — modernised
# here without changing the contract.
@asynccontextmanager
async def lifespan(app: FastAPI):
    removed = cleanup_old_sessions()
    if removed:
        logger.info("Session cleanup on startup: removed %d expired session(s)", removed)
    else:
        logger.info("Session cleanup on startup: nothing to remove")
    yield
    # No shutdown work today — sessions are persisted on disk and
    # the TTL sweep on the next startup is what bounds growth.


app = FastAPI(
    title="RFI Answer Builder API",
    description="Backend for the RFI Q&A web UI. Wraps the pipeline package.",
    lifespan=lifespan,
)

app.include_router(sessions.router)
app.include_router(ingest.router)
app.include_router(answer.router)
app.include_router(corpus.router)


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness probe. Returns {ok: true} when the app is up.

    Separate from /api/* so a reverse-proxy / k8s health check
    does not have to be allowlisted alongside business endpoints.
    """
    return {"ok": True}
