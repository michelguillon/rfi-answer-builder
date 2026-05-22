"""api.session — per-user session directory management.

Each browser session gets a UUID and a directory under ./tmp/:

  tmp/{session_id}/
    upload.xlsx       uploaded Excel (ingest or answer workflow)
    profile.json      profiler proposal awaiting human approval
    config.json       human-approved column→role mapping
    answers.json      generated answers (answer workflow)
    output.xlsx       filled RFI ready for download

ARCHITECTURAL DECISION: filesystem is the state store, not a database.

A single-purpose internal tool with predictable per-session state
does not need the durability, query, or migration story a database
provides. The filesystem is:

  - auditable    (each session is one inspectable directory);
  - debuggable   (you can `ls tmp/{id}/` and see exactly where a
                  workflow stalled);
  - migration-free (no schema, no Alembic, no init container);
  - operationally cheap to clean (delete the directory tree).

Concurrency between sessions is trivially safe because each session
writes to its own directory tree — no shared mutable file.
Concurrency *within* a session is not a concern because a session
maps 1:1 to a browser tab and the workflows are step-locked
(upload → profile → approve → ingest, etc.).

ARCHITECTURAL DECISION: 24-hour TTL, startup + hourly cleanup.

Sessions exist to hold an in-flight workflow's intermediate state;
they are not durable user data. 24 hours is generous for "user
walked away, came back tomorrow"; longer would mean accumulating
forgotten uploads of real client RFIs on disk. Shorter would risk
interrupting an actual lunch break.

Cleanup runs on app startup AND every hour via an asyncio task
spawned from the FastAPI lifespan. The original design (startup-
only) assumed daily restarts; the production deployment uses
`restart: unless-stopped` and survives for weeks, so a restart-
triggered sweep is effectively never. Promoting cleanup to a
background task is the exact upgrade LEARNING_NOTES entry 16
foresaw ("if sessions ever accumulate in practice, promote this
to an asyncio background task"). It keeps the cleanup logic
close to the code that creates sessions and travels with the
app rather than per-server cron config — one less artefact to
keep in sync across deployments. The 1-hour interval bounds
worst-case staleness to TTL + 1h = 25h.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

logger = logging.getLogger(__name__)

TMP_DIR = Path("./tmp")
SESSION_TTL_SECONDS = 24 * 60 * 60
CLEANUP_INTERVAL_SECONDS = 60 * 60


def create_session() -> str:
    """Create a fresh session directory and return its UUID."""
    TMP_DIR.mkdir(exist_ok=True)
    session_id = str(uuid4())
    (TMP_DIR / session_id).mkdir()
    return session_id


def get_session_dir(session_id: str) -> Path:
    """Return the session directory, raising 404 if it does not exist.

    The session_id is treated as opaque — no parsing, no validation
    beyond "does the directory exist". UUID4 is unguessable enough
    to serve as a capability token between the create_session call
    and subsequent requests from the same tab.

    Not an auth token: it grants access only to one ephemeral
    workflow directory, never to corpus data or other sessions.
    """
    session_dir = TMP_DIR / session_id
    if not session_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
    return session_dir


def cleanup_old_sessions() -> int:
    """Delete session directories whose mtime is older than the TTL.

    Returns the number of directories removed. Safe to call when
    ./tmp does not yet exist (returns 0).
    """
    if not TMP_DIR.is_dir():
        return 0
    cutoff = time.time() - SESSION_TTL_SECONDS
    removed = 0
    for session_dir in TMP_DIR.iterdir():
        if not session_dir.is_dir():
            continue
        if session_dir.stat().st_mtime < cutoff:
            shutil.rmtree(session_dir, ignore_errors=True)
            removed += 1
    return removed


async def cleanup_periodically(interval_seconds: int = CLEANUP_INTERVAL_SECONDS) -> None:
    """Sleep, sweep stale sessions, repeat forever.

    Spawned as an asyncio task from the FastAPI lifespan; cancelled
    cleanly on shutdown via the surrounding asynccontextmanager.

    Sleep first, then sweep: the lifespan already runs one synchronous
    cleanup at startup, so the first periodic sweep is one interval
    later — no double-sweep at boot.

    The cleanup itself is sync I/O (shutil.rmtree). Wrapped in
    asyncio.to_thread so a slow rmtree on a large session does not
    block the event loop while it's running.

    Errors from a single iteration are logged and swallowed so a
    transient filesystem hiccup cannot kill the loop for the rest
    of the deployment's lifetime — that would silently re-create
    the very accumulation problem this task exists to prevent.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            removed = await asyncio.to_thread(cleanup_old_sessions)
            if removed:
                logger.info(
                    "Periodic session cleanup: removed %d expired session(s)",
                    removed,
                )
        except Exception:  # noqa: BLE001 — keep the loop alive across transient errors
            logger.exception("Periodic session cleanup failed")
