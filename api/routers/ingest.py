"""api.routers.ingest — upload + profile + approve + ingest.

Stub in Step 1. Real behaviour (filled out in Steps 2 and 3):

  POST /api/ingest/upload    save Excel under tmp/{session_id}/
  GET  /api/ingest/profile   SSE stream of profiler steps + proposal
  POST /api/ingest/approve   write approved config, SSE stream of ingest
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


@router.get("")
async def ingest_stub() -> dict:
    return {"status": "stub", "endpoint": "GET /api/ingest"}
