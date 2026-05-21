"""api.routers.sessions — create sessions.

  POST /api/sessions       create a new session, return {session_id}

A session is a uuid + a tmp/{uuid}/ directory; see api.session for
the lifecycle. The session_id is a per-tab capability token, not
an auth token — see api/CLAUDE.md.
"""

from fastapi import APIRouter

from api.session import create_session

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.post("")
async def create_new_session() -> dict:
    """Create a new session directory under ./tmp and return its id."""
    session_id = create_session()
    return {"session_id": session_id}
