"""api.main — FastAPI app entry point.

Run via:
    docker compose up backend

Which executes:
    uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.chroma_client import CHROMA_IDLE_TTL, _cleanup_loop
from api.routers import answer, corpus, ingest, sessions
from api.session import cleanup_old_sessions, cleanup_periodically

logger = logging.getLogger("api.main")
logging.basicConfig(level=logging.INFO)


# ARCHITECTURAL DECISION: lifespan context manager, not @app.on_event.
#
# FastAPI 0.93+ deprecated @app.on_event("startup"/"shutdown") in
# favour of a single asynccontextmanager that wraps the application
# lifetime. The lifespan form keeps startup and shutdown in one
# place, can hold state in the contextmanager's local scope, and is
# what FastAPI will still support five versions from now. The
# rfi_SPEC snippet showed the older @app.on_event form — modernised
# here without changing the contract.
@asynccontextmanager
async def lifespan(app: FastAPI):
    removed = cleanup_old_sessions()
    if removed:
        logger.info("Session cleanup on startup: removed %d expired session(s)", removed)
    else:
        logger.info("Session cleanup on startup: nothing to remove")

    # Background task that re-runs cleanup every hour. The startup
    # sweep above only covers boot; production runs `restart:
    # unless-stopped` and survives for weeks, so periodic sweeping
    # is what actually bounds growth between restarts. See
    # api/session.py and LEARNING_NOTES entry 27 for the rationale.
    cleanup_task = asyncio.create_task(cleanup_periodically())

    # ChromaDB lazy load + idle eviction. The client is NOT loaded here —
    # it cold-loads on the first request (see api/chroma_client.py). This
    # thread only reclaims it after CHROMA_IDLE_TTL seconds of inactivity,
    # so an idle backend drops back to ~50MB instead of pinning ~1.2GB.
    # daemon=True so it never blocks process shutdown; TTL=0 disables
    # eviction entirely and the thread is not started.
    if CHROMA_IDLE_TTL > 0:
        t = threading.Thread(
            target=_cleanup_loop,
            daemon=True,
            name="chroma-idle-cleanup",
        )
        t.start()
        logger.info("ChromaDB idle cleanup thread started (TTL=%ds)", CHROMA_IDLE_TTL)
    else:
        logger.info("ChromaDB idle cleanup disabled (CHROMA_IDLE_TTL_SECONDS=0)")

    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


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
