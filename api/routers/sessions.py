"""api.routers.sessions — create / inspect sessions.

Stub in Step 1. Real behaviour:

  POST /api/sessions       create a new session, return {session_id}
  GET  /api/sessions/{id}  return whether the session is alive
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("")
async def list_sessions_stub() -> dict:
    return {"status": "stub", "endpoint": "GET /api/sessions"}


@router.post("")
async def create_session_stub() -> dict:
    return {"status": "stub", "endpoint": "POST /api/sessions"}
