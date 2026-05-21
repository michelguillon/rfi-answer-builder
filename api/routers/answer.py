"""api.routers.answer — upload + process + export.

Stub in Step 1. Real behaviour (filled out in Steps 4 and 5):

  POST /api/answer/upload    save new RFI under tmp/{session_id}/
  GET  /api/answer/process   SSE stream of per-question answers
  GET  /api/answer/export    download the filled Excel
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/answer", tags=["answer"])


@router.get("")
async def answer_stub() -> dict:
    return {"status": "stub", "endpoint": "GET /api/answer"}
