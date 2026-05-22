"""api.routers.corpus — read-only stats about the ingested corpus.

  GET /api/corpus/stats   {total_pairs, source_files, files: [...]}

Used by the Landing page footer to show "N Q&A pairs across M
source RFIs" without exposing the chunk-level vector store.
"""

from __future__ import annotations

import asyncio

import chromadb
from fastapi import APIRouter, HTTPException

from pipeline.ingest import CHROMA_PATH

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

    # Pull all metadatas in one shot. Chroma `.get()` without `ids` or
    # `where` returns every entry; for a <50k-chunk corpus this is
    # fine. If the corpus grows past that, we add a separate index
    # over source_files at ingest time and read from there instead.
    fetched = coll.get(include=["metadatas"])
    metadatas = fetched.get("metadatas") or []
    files = sorted({m.get("source_file", "?") for m in metadatas if m})

    return {
        "total_pairs": total_pairs,
        "source_files": len(files),
        "files": files,
    }


@router.get("/stats")
async def stats() -> dict:
    """Return total Q&A pair count + source RFI count for the corpus."""
    return await asyncio.to_thread(_read_stats)
