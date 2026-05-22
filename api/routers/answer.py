"""api.routers.answer — upload + process (SSE) + edit + export.

  POST /api/answer/upload?session_id=...     save Excel + extract questions
  GET  /api/answer/process?session_id=...    SSE stream of per-question answers
  POST /api/answer/edit                      persist user edits/skips
  GET  /api/answer/export?session_id=...     download the filled Excel
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from api.services.answerer import (
    QUESTIONS_FILENAME,
    UPLOAD_FILENAME,
    extract_questions,
    run_answer,
)
from api.services.exporter import ORIGINAL_NAME_FILENAME
from api.services.exporter import (
    build_output,
    get_download_name,
    update_answers_inplace,
)
from api.session import get_session_dir

router = APIRouter(prefix="/api/answer", tags=["answer"])

ALLOWED_EXTENSIONS = {".xlsx", ".xlsm"}
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


# ARCHITECTURAL DECISION: upload does the lightweight question
# extraction inline, not via SSE.
#
# Question extraction is cheap (~1s for a 200-row file) — no Mistral
# calls, just openpyxl + the heuristic_role classifier from
# pipeline.profile. Streaming it as SSE would buy nothing the user
# cares about; the user wants to confirm "yes, we found 47 questions"
# before kicking off the slow answer generation. A synchronous
# response that returns {question_count, questions_preview} is the
# fitting shape.
#
# The slow part (Mistral generation per question) is the GET
# /api/answer/process endpoint, which IS SSE — it can take minutes
# for a long RFI.
@router.post("/upload")
async def upload(
    file: UploadFile = File(...),
    session_id: str = Query(...),
) -> dict:
    """Save the upload, extract questions heuristically, persist them.

    Returns {session_id, filename, question_count, questions_preview}.
    """
    if file.filename is None:
        raise HTTPException(400, "Missing filename")
    suffix = "." + file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            400,
            f"Only {sorted(ALLOWED_EXTENSIONS)} files accepted, got '{file.filename}'",
        )

    session_dir = get_session_dir(session_id)
    upload_path = session_dir / UPLOAD_FILENAME
    contents = await file.read()
    upload_path.write_bytes(contents)

    # Persist the original filename so the export endpoint can name
    # the download "<stem>_answered.xlsx". Same pattern as the ingest
    # upload — keeps the backend authoritative on filenames.
    (session_dir / ORIGINAL_NAME_FILENAME).write_text(
        file.filename, encoding="utf-8"
    )

    try:
        extracted = await asyncio.to_thread(extract_questions, upload_path)
    except ValueError as exc:
        # Can't find the question column — surface a 422 so the
        # frontend can prompt the user (re-upload, or a future
        # "specify question column manually" path).
        raise HTTPException(422, str(exc))

    # Persist for the process step to read.
    (session_dir / QUESTIONS_FILENAME).write_text(
        json.dumps(extracted, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    questions = extracted["questions"]
    preview = [q["text"] for q in questions[:3]]
    return {
        "session_id": session_id,
        "filename": file.filename,
        "question_count": len(questions),
        "question_column": extracted["question_column"],
        "question_column_header": extracted["question_column_header"],
        "sheet": extracted["sheet"],
        "header_row": extracted["header_row"],
        "questions_preview": preview,
    }


@router.get("/process")
async def process(session_id: str = Query(...)) -> StreamingResponse:
    """Stream per-question answer events as Server-Sent Events.

    Yields progress, answer, done. On error yields a single error
    event and closes the stream. The full answers list is persisted
    to tmp/{session_id}/answers.json on done — the export endpoint
    reads it.
    """
    session_dir = get_session_dir(session_id)

    async def stream():
        async for event in run_answer(session_dir):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


# ARCHITECTURAL DECISION: edits are POSTed as their own step; the
# export endpoint stays a plain GET.
#
# SPEC_UI Step 9's ExportButton calls "POST edits to backend then
# GET /api/answer/export, trigger browser download". A GET-only
# download has two practical advantages over a POST that returns
# the file:
#   - Browsers handle GET downloads cleanly (history, retry, file
#     name from Content-Disposition).
#   - The exporter reads its inputs entirely from disk
#     (answers.json with _status flags set by /edit), so the export
#     can be re-run idempotently without re-sending the edits.
#
# The edit step persists per-answer _status into answers.json:
#   - "edited":   answer text replaced
#   - "skipped":  cells will be blank in the export
#   - "accepted" (default): write generated answer as-is
# After /edit, the answers.json on disk is the single source of
# truth — the export reads it once.
class EditBody(BaseModel):
    """Body of POST /api/answer/edit.

    `overrides` maps the answer's `index` (1-based, matches the
    progress/answer SSE events) to the user's edited text.
    `skipped` lists indices the user explicitly skipped. Indices
    not in either list keep their original generated answer.
    """

    session_id: str
    overrides: dict[int, str] = {}
    skipped: list[int] = []


@router.post("/edit")
async def edit(body: EditBody) -> dict:
    """Persist the user's per-answer edits and skips into answers.json."""
    session_dir = get_session_dir(body.session_id)
    try:
        modified = await asyncio.to_thread(
            update_answers_inplace, session_dir, body.overrides, body.skipped
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    return {"modified": modified, "session_id": body.session_id}


@router.get("/export")
async def export(session_id: str = Query(...)) -> FileResponse:
    """Build (or rebuild) output.xlsx and stream it as a download."""
    session_dir = get_session_dir(session_id)
    try:
        output_path = await asyncio.to_thread(build_output, session_dir)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    download_name = get_download_name(session_dir)
    return FileResponse(
        path=str(output_path),
        filename=download_name,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )
