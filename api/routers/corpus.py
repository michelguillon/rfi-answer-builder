"""api.routers.corpus — read + delete operations on the ingested corpus.

  GET    /api/corpus/stats             total + per-file chunk counts
  DELETE /api/corpus/rfi?source_file=X remove an RFI from all collections

The Landing page uses /stats for the footer table; the delete
button hits /rfi. Both keep the chunk-level vector store hidden
behind summary numbers.
"""

from __future__ import annotations

import asyncio
import glob
import json
import re
from pathlib import Path

import chromadb
from fastapi import APIRouter, HTTPException, Query

from pipeline.ingest import CHROMA_PATH, COLLECTIONS

router = APIRouter(prefix="/api/corpus", tags=["corpus"])

# ARCHITECTURAL DECISION: report stats from rfi_combined_cosine,
# not the production-recommended rfi_separated_cosine.
#
# Both collections see every RFI ingested, but separated stores
# TWO chunks per Q&A pair (one question, one answer). Combined
# stores ONE chunk per pair (Q+A bundled). For a "how big is the
# corpus" number, combined.count() == number of Q&A pairs without
# any deduplication. Reading it from separated would require
# counting distinct pair_ids in metadata — slower and more code
# for the same number.
#
# This is a read-only choice for display. Production retrieval
# still uses rfi_separated_cosine per LEARNING_NOTES entry 13.
STATS_COLLECTION = "rfi_combined_cosine"
CHECKPOINT_PATH = Path("outputs/.ingest_checkpoint.json")


def _slugify(filename: str) -> str:
    """Mirror pipeline.profile.slugify_for_config so the config_rfi_*
    filename the delete endpoint removes matches what ingest wrote."""
    stem = Path(filename).stem.lower()
    slug = re.sub(r"[^a-z0-9]+", "_", stem)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "untitled"


def _read_stats() -> dict:
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    try:
        coll = client.get_collection(STATS_COLLECTION)
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Collection {STATS_COLLECTION!r} not found ({exc}). "
                f"Ingest at least one RFI first."
            ),
        )

    total_pairs = coll.count()
    fetched = coll.get(include=["metadatas"])
    metadatas = fetched.get("metadatas") or []

    # Per-file chunk count. Pull from rfi_combined_cosine so the
    # count is "Q&A pairs", not "vector chunks" — matches what the
    # user wrote on disk.
    per_file: dict[str, int] = {}
    for m in metadatas:
        if not m:
            continue
        src = m.get("source_file", "?")
        per_file[src] = per_file.get(src, 0) + 1

    return {
        "total_pairs": total_pairs,
        "source_files": len(per_file),
        "files": [
            {"source_file": name, "chunks": per_file[name]}
            for name in sorted(per_file.keys())
        ],
    }


@router.get("/stats")
async def stats() -> dict:
    """Return total Q&A pairs + per-file chunk counts."""
    return await asyncio.to_thread(_read_stats)


# ARCHITECTURAL DECISION: delete removes chunks + checkpoint
# entries + the config_rfi_<slug>.json file, but KEEPS the
# data/<filename>.xlsx upload on disk.
#
# Reasoning:
#   - Removing chunks across all 4 collections is the user's
#     actual intent ("remove from corpus").
#   - Removing the checkpoint entries prevents a future
#     `python -m pipeline.ingest` from "helpfully" re-adding the
#     chunks the user just deleted.
#   - Removing the config_rfi_<slug>.json prevents the CLI from
#     even SEEING this RFI as ingestable — without the config the
#     CLI's load_all_rows() skips the file entirely.
#   - The data/<filename>.xlsx file is small and harmless if it
#     lingers. The user can always re-upload via the UI to
#     re-ingest; keeping the file means they can also bypass the
#     re-upload by manually re-creating a config. Deleting the
#     bytes would foreclose both paths.
#
# A future `?also_delete_file=1` query param could nuke the data
# file too. Not added today because nobody is asking and the
# minimal version is less destructive.
def _delete_rfi(source_file: str) -> dict:
    if not source_file or "/" in source_file or "\\" in source_file:
        raise HTTPException(400, f"Invalid source_file: {source_file!r}")

    client = chromadb.PersistentClient(path=CHROMA_PATH)

    chunks_removed: dict[str, int] = {}
    for coll_name in COLLECTIONS:
        try:
            coll = client.get_collection(coll_name)
        except Exception:
            chunks_removed[coll_name] = 0
            continue
        before = coll.count()
        coll.delete(where={"source_file": source_file})
        after = coll.count()
        chunks_removed[coll_name] = before - after

    total_chunks_removed = sum(chunks_removed.values())
    if total_chunks_removed == 0:
        raise HTTPException(
            404,
            f"No chunks with source_file={source_file!r} found in any collection.",
        )

    # Drop checkpoint entries for this file.
    checkpoint_entries_removed = 0
    if CHECKPOINT_PATH.exists():
        state = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        before = len(state.get("completed", []))
        state["completed"] = [
            c for c in state.get("completed", [])
            if c.get("source_file") != source_file
        ]
        checkpoint_entries_removed = before - len(state["completed"])
        CHECKPOINT_PATH.write_text(
            json.dumps(state, indent=2) + "\n", encoding="utf-8"
        )

    # Drop the config file so the CLI can't pick this RFI up again.
    config_path = Path(f"config_rfi_{_slugify(source_file)}.json")
    config_removed = False
    if config_path.exists():
        config_path.unlink()
        config_removed = True

    return {
        "source_file": source_file,
        "chunks_removed": chunks_removed,
        "total_chunks_removed": total_chunks_removed,
        "checkpoint_entries_removed": checkpoint_entries_removed,
        "config_removed": config_removed,
        "config_path": str(config_path),
    }


@router.delete("/rfi")
async def delete_rfi(source_file: str = Query(...)) -> dict:
    """Remove all chunks + checkpoint + config for one RFI."""
    return await asyncio.to_thread(_delete_rfi, source_file)
