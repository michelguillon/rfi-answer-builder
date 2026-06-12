"""api.services.ingester — wrap pipeline.ingest as an SSE stream.

Yields events in this order, repeated per collection:

    {"type": "collection", "data": "rfi_combined_cosine"}
    {"type": "progress",   "data": {"collection": "...", "batch": 1, "total": 9}}
    ...
    {"type": "complete",   "data": {"collection": "...", "chunks": 140}}

Then once at the end:

    {"type": "done", "data": {"total_chunks": 557, "corpus_size": 1786}}

If anything fails (Excel parse, Mistral embed, ChromaDB write):

    {"type": "error", "data": "<message>"}

The service uses the existing pipeline.ingest helpers (BATCH_SIZE,
COLLECTIONS, embed_batch, chunk_id, sanitize_metadata, the
checkpoint accessors) so the UI-driven ingest and the CLI-driven
ingest write the same data to the same collections with the same
checkpoint discipline. The only thing the service does NOT reuse
is `ingest_file`, because that function logs to stdout and does
not yield — we re-implement the embed loop here with an event
yielded per batch.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import AsyncGenerator

from api.chroma_client import get_chroma_client
from pipeline.ingest import (
    BATCH_SIZE,
    COLLECTIONS,
    chunk_id,
    embed_batch,
    is_completed,
    load_checkpoint,
    mark_completed,
    sanitize_metadata,
)
from pipeline.loaders import load_excel
from pipeline.mistral_helpers import get_client
from pipeline.profile import (
    ColumnProfile,
    MappingProposal,
    SheetProfile,
    build_config,
    slugify_for_config,
)
from pipeline.review_chunks import build_combined_chunks, build_separated_chunks

PROFILE_FILENAME = "profile.json"
UPLOAD_FILENAME = "upload.xlsx"
CONFIG_FILENAME = "config.json"
ORIGINAL_NAME_FILENAME = "original_filename"


# ARCHITECTURAL DECISION: two persisted copies of the config.
#
# The session dir gets `tmp/{sid}/config.json` because the session
# is the auditable record of the workflow — you can `ls tmp/{sid}/`
# and see upload → profile → config in one place.
#
# The repo root gets `config_rfi_<slug>.json` because that is what
# the CLI ingest looks for (`glob.glob("config_rfi_*.json")` in
# pipeline.ingest.load_all_rows). A future `python -m pipeline.ingest
# --reset` must be able to rebuild ChromaDB from the on-disk
# corpus without depending on tmp/ (which is cleaned up nightly).
#
# Both files are byte-identical. Drift between them would mean the
# CLI re-ingest produces different chunks than the UI ingest just
# produced — bad. The single write_text below produces both.
def _build_config_from_profile(
    profile_data: dict,
    original_filename: str,
) -> dict:
    """Reconstruct minimal MappingProposal + SheetProfile shells for
    pipeline.profile.build_config, then return its dict output.

    We don't keep the original SheetProfile object across the
    upload→profile→approve flow (it lives only in profiler.py's
    function scope). The on-disk profile.json has everything
    build_config actually needs — column letters, roles, sheet
    name, header_row, inferred client/date. The stats fields on
    ColumnProfile (pct_non_empty, avg_words, etc.) are not used
    by build_config, so we pass zero placeholders.
    """
    proposal = MappingProposal(
        sheet=profile_data["sheet"],
        column_roles=profile_data["column_roles"],
        client=profile_data.get("client"),
        date=profile_data.get("date"),
        reasoning=profile_data.get("reasoning", ""),
    )
    sheet_profile = SheetProfile(
        name=profile_data["sheet"],
        n_data_rows=0,
        n_cols=len(profile_data["columns"]),
        columns=[
            ColumnProfile(
                letter=c["letter"],
                header=c.get("header", ""),
                non_empty_count=0,
                pct_non_empty=0.0,
                avg_word_count=0.0,
                question_mark_rate=0.0,
                cardinality=0,
                sample_values=c.get("samples", []),
                heuristic_role=c.get("heuristic_role", ""),
                heuristic_reason="",
            )
            for c in profile_data["columns"]
        ],
    )
    return build_config(
        proposal,
        sheet_profile,
        source_path=Path(original_filename),
        header_row=profile_data["header_row"],
    )


async def run_ingest(
    session_dir: Path,
    client_edit: str | None,
    date_edit: str | None,
) -> AsyncGenerator[dict, None]:
    """Stream ingest events: collection / progress / complete / done / error."""

    # ── 1. Read profile.json + original filename ──────────────────────
    profile_path = session_dir / PROFILE_FILENAME
    upload_path = session_dir / UPLOAD_FILENAME
    original_path = session_dir / ORIGINAL_NAME_FILENAME

    if not profile_path.exists():
        yield {"type": "error", "data": "No profile.json — run the profile step first"}
        return
    if not upload_path.exists():
        yield {"type": "error", "data": f"No upload at {upload_path}"}
        return
    if not original_path.exists():
        yield {"type": "error", "data": "Original filename sidecar missing — re-upload"}
        return

    try:
        profile_data = json.loads(profile_path.read_text(encoding="utf-8"))
        original_filename = original_path.read_text(encoding="utf-8").strip()

        # Apply the user's edits to client/date.
        if client_edit is not None:
            profile_data["client"] = client_edit or None
        if date_edit is not None:
            profile_data["date"] = date_edit or None

        # ── 2. Build config + persist (session + repo root) ──────────────
        config = _build_config_from_profile(profile_data, original_filename)
        config_json = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
        (session_dir / CONFIG_FILENAME).write_text(config_json, encoding="utf-8")
        slug = slugify_for_config(Path(original_filename))
        Path(f"config_rfi_{slug}.json").write_text(config_json, encoding="utf-8")

        # ── 3. Copy upload.xlsx into data/<original_filename> ────────────
        # ARCHITECTURAL DECISION: copy, not move. Workflow integrity
        # requires the upload to stay in the session dir until cleanup
        # so a re-approve (e.g. after a network glitch) can re-run
        # without asking the user to re-upload.
        data_dir = Path("data")
        data_dir.mkdir(exist_ok=True)
        data_path = data_dir / original_filename
        if not data_path.exists() or data_path.stat().st_size != upload_path.stat().st_size:
            shutil.copy(str(upload_path), str(data_path))

        # ── 4. Load rows once (no API calls — cheap) ──────────────────────
        rows = await asyncio.to_thread(load_excel, str(data_path), config)
        if not rows:
            yield {"type": "error", "data": "Loader returned zero rows — check the column mapping"}
            return

        # ── 5. ChromaDB + Mistral clients ─────────────────────────────────
        chroma_client = await asyncio.to_thread(get_chroma_client)
        mistral_client = get_client()
        state = load_checkpoint()

        total_chunks_this_ingest = 0

        # ── 6. Iterate the 4 collections ──────────────────────────────────
        for coll_name, spec in COLLECTIONS.items():
            yield {"type": "collection", "data": coll_name}
            coll = await asyncio.to_thread(
                chroma_client.get_or_create_collection,
                name=coll_name,
                metadata={"hnsw:space": spec["space"]},
            )

            if is_completed(state, coll_name, original_filename):
                yield {
                    "type": "complete",
                    "data": {
                        "collection": coll_name,
                        "chunks": 0,
                        "note": "already in checkpoint — skipped",
                    },
                }
                continue

            chunks = (
                build_combined_chunks(rows)
                if spec["strategy"] == "combined"
                else build_separated_chunks(rows)
            )
            non_empty = [c for c in chunks if c["text"].strip()]
            if not non_empty:
                mark_completed(state, coll_name, original_filename, len(rows), 0)
                yield {"type": "complete", "data": {"collection": coll_name, "chunks": 0}}
                continue

            total_batches = (len(non_empty) + BATCH_SIZE - 1) // BATCH_SIZE
            for i in range(0, len(non_empty), BATCH_SIZE):
                batch = non_empty[i : i + BATCH_SIZE]
                texts = [c["text"] for c in batch]
                vectors = await asyncio.to_thread(embed_batch, mistral_client, texts)
                await asyncio.to_thread(
                    coll.add,
                    documents=texts,
                    metadatas=[sanitize_metadata(c["metadata"]) for c in batch],
                    embeddings=vectors,
                    ids=[chunk_id(c) for c in batch],
                )
                yield {
                    "type": "progress",
                    "data": {
                        "collection": coll_name,
                        "batch": (i // BATCH_SIZE) + 1,
                        "total": total_batches,
                    },
                }

            mark_completed(state, coll_name, original_filename, len(rows), len(non_empty))
            total_chunks_this_ingest += len(non_empty)
            yield {
                "type": "complete",
                "data": {"collection": coll_name, "chunks": len(non_empty)},
            }

        # ── 7. Final corpus-wide totals ───────────────────────────────────
        corpus_size = 0
        for name in COLLECTIONS:
            try:
                c = await asyncio.to_thread(chroma_client.get_collection, name)
                corpus_size += await asyncio.to_thread(c.count)
            except Exception:  # noqa: BLE001 — missing collection is fine
                pass

        yield {
            "type": "done",
            "data": {
                "total_chunks": total_chunks_this_ingest,
                "corpus_size": corpus_size,
            },
        }

    except Exception as exc:  # noqa: BLE001 — surface all failures as an event
        yield {"type": "error", "data": f"{type(exc).__name__}: {exc}"}
