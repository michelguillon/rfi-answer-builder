"""
ingest_rfi.py — embed RFI chunks into ChromaDB  [spec Step 5]
==============================================================
Reads every `config_rfi_*.json`, loads each file via the Step-3
loader, builds chunks via the Step-4 shared builders, embeds with
`mistral-embed`, and writes to four ChromaDB collections — one per
(strategy × distance metric) combination defined in the spec:

  rfi_combined_cosine    Strategy A, cosine
  rfi_combined_l2        Strategy A, L2
  rfi_separated_cosine   Strategy B, cosine
  rfi_separated_l2       Strategy B, L2

After each file is fully ingested into a given collection, we
record the (collection, source_file) pair in a checkpoint file.
A re-run skips already-completed work; `--reset` drops all four
collections and the checkpoint and starts over. `--collection NAME`
limits work to one collection — used for the first verification run
before committing to all four.

CLI:
    docker compose run --rm pipeline python ingest_rfi.py
    docker compose run --rm pipeline python ingest_rfi.py --reset
    docker compose run --rm pipeline python ingest_rfi.py --collection rfi_combined_cosine

----------------------------------------------------------------------
ARCHITECTURAL DECISION: four collections, one per (strategy × metric).
ChromaDB's distance metric is set at collection-creation time and is
immutable thereafter. The experiment matrix in the spec requires
comparing cosine vs L2 AND combined vs separated. The clean solution
is to build all four collections up front; switching metric at query
time is not possible. Storage cost is small — 1024-dim vectors × the
chunk count are negligible compared to client embedding budgets.

ARCHITECTURAL DECISION: per-file checkpoint, save after every file.
The unit of resumable work is one (collection, source_file) pair.
After each file's chunks are embedded and added to ChromaDB, the
checkpoint file `outputs/.ingest_checkpoint.json` records the
completion. If the process crashes mid-corpus or `call_with_retry`
gives up on a sustained 429/5xx, re-running picks up exactly where
the previous run stopped. The granularity matches the failure mode —
embedding a single file is the right amount of work to lose to a
transient error, not too much (we don't want to re-embed thousands
of chunks) and not too little (we don't want one checkpoint per
batch of 16 chunks).

ARCHITECTURAL DECISION: drop empty-text chunks before embedding.
The Step-3 loader keeps "asked but unanswered" rows (question
present, answer blank). Strategy A bundles those into a single
chunk that still has a non-empty question. Strategy B emits a
separate answer chunk whose text is the empty string. Sending an
empty string to `mistral-embed` either errors out or returns a
zero vector — either way the chunk pollutes retrieval. The
ingester filters empty-text chunks before the batch; the paired
question chunk is still embedded normally. The reviewer surfaces
this in `min_words: 0` on Strategy B's stats so the human sees
the filtering happen.

ARCHITECTURAL DECISION: stable chunk IDs based on pair_id (+ role).
ChromaDB IDs must be unique per collection. Format:
  Strategy A combined:   "<pair_id>"
  Strategy B separated:  "<pair_id>__question" / "<pair_id>__answer"
pair_id is globally unique (slug includes the filename), so these
identifiers are stable across re-ingests. Re-running ingest on the
same data would attempt to add the same IDs and ChromaDB would
raise on duplicates — which is fine because the checkpoint prevents
re-ingest of completed (collection, source_file) pairs in the first
place.

ARCHITECTURAL DECISION: sanitise metadata before ChromaDB add.
ChromaDB requires metadata values to be str/int/float/bool. None
and empty strings are not accepted in all versions. The loader
sometimes carries date=None when no date was inferred from the
filename. We strip None/empty before add — the metadata loss is
"this row has no date", which is true and shouldn't pollute the
metadata schema with sentinel values.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path
from typing import Any

import chromadb

from loaders import load_excel
from mistral_helpers import call_with_retry, get_client
from review_rfi_chunks import build_combined_chunks, build_separated_chunks


# ─── Configuration ──────────────────────────────────────────────────────
CHROMA_PATH = "./chroma_db"
CHECKPOINT_PATH = Path("outputs/.ingest_checkpoint.json")
EMBED_MODEL = "mistral-embed"
BATCH_SIZE = 16

# (strategy, distance) → collection name. The four collections the
# experiment matrix requires.
COLLECTIONS: dict[str, dict[str, str]] = {
    "rfi_combined_cosine":   {"strategy": "combined",  "space": "cosine"},
    "rfi_combined_l2":       {"strategy": "combined",  "space": "l2"},
    "rfi_separated_cosine":  {"strategy": "separated", "space": "cosine"},
    "rfi_separated_l2":      {"strategy": "separated", "space": "l2"},
}


# ─── Checkpoint ─────────────────────────────────────────────────────────
def load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        with open(CHECKPOINT_PATH) as f:
            return json.load(f)
    return {"completed": []}


def save_checkpoint(state: dict) -> None:
    CHECKPOINT_PATH.parent.mkdir(exist_ok=True)
    CHECKPOINT_PATH.write_text(json.dumps(state, indent=2) + "\n",
                               encoding="utf-8")


def is_completed(state: dict, collection: str, source_file: str) -> bool:
    return any(
        c["collection"] == collection and c["source_file"] == source_file
        for c in state["completed"]
    )


def mark_completed(state: dict, collection: str, source_file: str,
                   rows: int, chunks_embedded: int) -> None:
    state["completed"].append({
        "collection": collection,
        "source_file": source_file,
        "rows": rows,
        "chunks_embedded": chunks_embedded,
    })
    save_checkpoint(state)


# ─── Helpers ────────────────────────────────────────────────────────────
def chunk_id(chunk: dict) -> str:
    """Stable unique identifier for a chunk within its collection."""
    pid = chunk["metadata"]["pair_id"]
    role = chunk["metadata"].get("role")
    return f"{pid}__{role}" if role else pid


def sanitize_metadata(meta: dict) -> dict:
    """ChromaDB rejects None / empty values in some versions. Drop them
    rather than coerce to a sentinel — the absence of a value is the
    correct semantic (this row had no date inferred, etc.)."""
    return {k: v for k, v in meta.items()
            if v is not None and v != ""}


def embed_batch(mistral_client, texts: list[str]) -> list[list[float]]:
    """One batched embedding call wrapped in retry. Returns vectors in
    the same order as the input texts."""
    response = call_with_retry(
        mistral_client.embeddings.create,
        model=EMBED_MODEL,
        inputs=texts,
    )
    return [d.embedding for d in response.data]


# ─── Ingestion per (collection, file) ──────────────────────────────────
def ingest_file(collection, chunks: list[dict],
                mistral_client) -> int:
    """Embed `chunks` in batches and add them to the collection.
    Empty-text chunks are filtered out before the call.
    Returns the count of chunks actually embedded."""
    # Drop empty-text chunks (Strategy B answer chunks where the row
    # had no answer). See ARCHITECTURAL DECISION block above.
    non_empty = [c for c in chunks if c["text"].strip()]
    if not non_empty:
        return 0

    for i in range(0, len(non_empty), BATCH_SIZE):
        batch = non_empty[i:i + BATCH_SIZE]
        texts = [c["text"] for c in batch]
        vectors = embed_batch(mistral_client, texts)
        collection.add(
            documents=texts,
            metadatas=[sanitize_metadata(c["metadata"]) for c in batch],
            embeddings=vectors,
            ids=[chunk_id(c) for c in batch],
        )
        print(f"      embedded {min(i + BATCH_SIZE, len(non_empty))}/"
              f"{len(non_empty)}")
    return len(non_empty)


# ─── Top-level run ──────────────────────────────────────────────────────
def load_all_rows() -> dict[str, list]:
    """Read every config and load its rows via load_excel. Returns
    {source_file: rows} in deterministic (sorted) order."""
    rows_by_file: dict[str, list] = {}
    for cfg_path in sorted(glob.glob("config_rfi_*.json")):
        with open(cfg_path) as f:
            cfg = json.load(f)
        xlsx_path = Path("data") / cfg["source_file"]
        rows = load_excel(str(xlsx_path), cfg)
        rows_by_file[cfg["source_file"]] = rows
    return rows_by_file


def reset_all(chroma_client) -> None:
    """Drop all four collections + clear the checkpoint."""
    for name in COLLECTIONS:
        try:
            chroma_client.delete_collection(name)
            print(f"  deleted collection: {name}")
        except (ValueError, Exception) as exc:
            # Collection didn't exist — fine.
            print(f"  (no existing collection {name}: {exc})")
    if CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
        print(f"  cleared checkpoint: {CHECKPOINT_PATH}")


def print_final_summary(chroma_client, collection_names: list[str]) -> None:
    print("\n" + "=" * 80)
    print("Final collection counts:")
    for name in collection_names:
        try:
            coll = chroma_client.get_collection(name)
            print(f"  {name:<28}  {coll.count():>5} chunks")
        except Exception as exc:
            print(f"  {name:<28}  (not created — {exc})")
    print("=" * 80)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Embed RFI chunks into ChromaDB collections.")
    parser.add_argument(
        "--reset", action="store_true",
        help="Drop all 4 collections + clear checkpoint, then re-ingest.")
    parser.add_argument(
        "--collection", default=None,
        help=f"Only ingest into this collection. One of: "
             f"{', '.join(COLLECTIONS.keys())}.")
    args = parser.parse_args(argv)

    if args.collection and args.collection not in COLLECTIONS:
        print(f"ERROR: unknown collection {args.collection!r}. "
              f"Choose from: {', '.join(COLLECTIONS.keys())}",
              file=sys.stderr)
        return 2

    print("=" * 80)
    print("RFI Ingestion — embed and store in ChromaDB")
    print("=" * 80)

    # 1. Load all rows once. Inexpensive (no API calls), so do up front.
    print("\nLoading rows from all config_rfi_*.json files...")
    try:
        rows_by_file = load_all_rows()
    except (ValueError, FileNotFoundError) as exc:
        print(f"\nERROR loading rows: {exc}", file=sys.stderr)
        return 3
    total_rows = sum(len(r) for r in rows_by_file.values())
    print(f"  loaded {total_rows} rows across {len(rows_by_file)} files")

    # 2. ChromaDB + Mistral clients.
    chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)
    mistral_client = get_client()

    # 3. Reset if asked.
    if args.reset:
        print("\nReset requested:")
        reset_all(chroma_client)

    # 4. Decide which collections to process.
    target_collections = (
        {args.collection: COLLECTIONS[args.collection]}
        if args.collection else COLLECTIONS
    )

    state = load_checkpoint()

    # 5. Ingest each (collection, file) pair.
    for coll_name, spec in target_collections.items():
        print(f"\n— Collection: {coll_name}  "
              f"(strategy={spec['strategy']}, space={spec['space']}) —")
        coll = chroma_client.get_or_create_collection(
            name=coll_name,
            metadata={"hnsw:space": spec["space"]},
        )
        # The space metadata is honoured on initial creation only —
        # `get_or_create_collection` returns the existing collection
        # if it exists, ignoring the new metadata. That's fine here
        # because the checkpoint prevents accidental re-ingest with
        # a different metric.
        for source_file, rows in rows_by_file.items():
            if is_completed(state, coll_name, source_file):
                print(f"    [skip] {source_file} (already in checkpoint)")
                continue
            chunks = (build_combined_chunks(rows)
                      if spec["strategy"] == "combined"
                      else build_separated_chunks(rows))
            print(f"    [{source_file}]  rows={len(rows)}  "
                  f"chunks={len(chunks)}  embedding...")
            try:
                n_embedded = ingest_file(coll, chunks, mistral_client)
            except Exception as exc:  # noqa: BLE001 — see decision block
                print(f"\nERROR ingesting {source_file} into {coll_name}: "
                      f"{exc}", file=sys.stderr)
                print(f"  Checkpoint preserves progress so far. "
                      f"Re-run to resume.", file=sys.stderr)
                return 4
            mark_completed(state, coll_name, source_file,
                           len(rows), n_embedded)
            print(f"      -> {n_embedded} embedded (skipped "
                  f"{len(chunks) - n_embedded} empty-text)")

    # 6. Final per-collection counts.
    print_final_summary(chroma_client, list(target_collections.keys()))
    print(f"\nCheckpoint: {CHECKPOINT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
