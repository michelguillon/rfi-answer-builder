"""
review_rfi_chunks.py — RFI chunk preview & approval gate  [spec Step 4]
========================================================================
Reads every `config_rfi_*.json` in the project root, loads each Excel
file via `loaders.load_excel`, and prints the chunks that the ingest
step would produce under each of the two strategies the spec defines:

  Strategy A — Combined.  One chunk per row.
      text = "Q: <question>\\nA: <answer>"   (+ "\\nC: <context>" if present)
      metadata = row metadata + {pair_id, strategy: "combined",
                                  source_file, chunk_index}

  Strategy B — Separated.  Two chunks per row, linked by pair_id.
      question chunk: text = "<question>",
                      metadata = {..., role: "question",
                                       strategy: "separated"}
      answer   chunk: text = "<answer>",
                      metadata = {..., role: "answer",
                                       strategy: "separated"}

The reviewer is **read-only**: no ChromaDB call, no embedding call, no
file write. It prints per-file row counts, per-strategy aggregate
stats (chunk count, avg/min/max word count), sample chunks from the
first and last loaded row, and then prompts for a y/n confirmation.
The 'y' / 'n' is a human gate before running `ingest_rfi.py`; this
script itself never triggers ingestion.

CLI:
    python review_rfi_chunks.py
    # ↑ run inside the pipeline container.

----------------------------------------------------------------------
ARCHITECTURAL DECISION: render the exact chunks the ingest step will
embed, do not paraphrase. The reviewer's job is to surface "what is
about to be sent to mistral-embed" so a human can spot a malformed
chunk before it lands in ChromaDB. Showing pretty-formatted previews
that differ from the real chunk text would defeat the purpose. So
this module reuses the same `build_combined_chunks` / `build_separated_chunks`
functions ingest will call, and prints their output verbatim.

ARCHITECTURAL DECISION: chunks are dicts (text + metadata), not a
dedicated dataclass. ChromaDB accepts (documents, metadatas) lists
directly. A `Chunk` dataclass would add a layer with no behaviour;
the dict matches ChromaDB's API exactly and lets the same builder
functions feed both the reviewer (this module) and the ingester
(Step 5). One representation, two consumers.

ARCHITECTURAL DECISION: context is appended to Strategy-A chunks
only. Per the spec's Decision 3 and the human's confirmation:
combined chunks include "\\nC: <context>" when context is present;
separated chunks keep their question and answer text clean (no
context concatenation) and the context value stays on the Row's
metadata for filtered retrieval. Mixing context into a separated
question/answer chunk would dilute the question-to-question and
answer-text signal those chunks are meant to capture.

ARCHITECTURAL DECISION: read-only, in-memory only. No write, no DB,
no Mistral call. The reviewer is a check-before-action. Ingestion
is Step 5. Separating "review" from "execute" lets the human
iterate (re-profile, re-config, re-review) without consuming
embedding API calls or polluting the vector store.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path
from typing import Any

from pipeline.loaders import load_excel
from pipeline.models import Row


# ─── Chunk builders (also called by ingest in Step 5) ──────────────────
def build_combined_chunks(rows: list[Row]) -> list[dict]:
    """Strategy A: one chunk per row, Q+A (and optional context) together.

    text format: "Q: <question>\\nA: <answer>" with "\\nC: <context>"
    appended only if the Row carries a non-empty context. The "Q: " /
    "A: " prefixes are intentional structural cues — the embedding
    model gets a small "this is a Q&A pair" signal that bare text
    would not provide.
    """
    chunks: list[dict] = []
    for idx, row in enumerate(rows):
        parts = [f"Q: {row.question}", f"A: {row.answer}"]
        if row.context:
            parts.append(f"C: {row.context}")
        chunks.append({
            "text": "\n".join(parts),
            "metadata": {
                **row.metadata,
                "pair_id": row.pair_id,
                "source_file": row.source_file,
                "strategy": "combined",
                "chunk_index": idx,
            },
        })
    return chunks


def build_separated_chunks(rows: list[Row]) -> list[dict]:
    """Strategy B: two chunks per row, linked by pair_id.

    The list is flat — Q chunks and A chunks interleaved in row order.
    Downstream code uses metadata['role'] to distinguish them and
    metadata['pair_id'] to fetch the paired counterpart after a
    question-side retrieval. Returns 2 * len(rows) chunks.
    """
    chunks: list[dict] = []
    chunk_index = 0
    for row in rows:
        base = {
            **row.metadata,
            "pair_id": row.pair_id,
            "source_file": row.source_file,
            "strategy": "separated",
        }
        chunks.append({
            "text": row.question,
            "metadata": {**base, "role": "question", "chunk_index": chunk_index},
        })
        chunk_index += 1
        chunks.append({
            "text": row.answer,
            "metadata": {**base, "role": "answer", "chunk_index": chunk_index},
        })
        chunk_index += 1
    return chunks


# ─── Stats ──────────────────────────────────────────────────────────────
def chunk_stats(chunks: list[dict]) -> dict:
    """Word-count aggregates over a list of chunks. Empty list returns zeros."""
    if not chunks:
        return {"count": 0, "avg_words": 0.0, "min_words": 0, "max_words": 0}
    counts = [len(c["text"].split()) for c in chunks]
    return {
        "count": len(chunks),
        "avg_words": sum(counts) / len(counts),
        "min_words": min(counts),
        "max_words": max(counts),
    }


def _truncate(text: str, n: int = 240) -> str:
    """Compact a chunk for printing. Preserves newlines as ' | '."""
    flat = text.replace("\n", " | ").strip()
    return flat if len(flat) <= n else flat[: n - 1] + "…"


# ─── Rendering ──────────────────────────────────────────────────────────
def print_per_file_summary(rows_by_file: dict[str, list[Row]]) -> None:
    print("\nPer-file row counts (loaded by loaders.load_excel):")
    print("  " + "─" * 86)
    total = 0
    for filename, rows in sorted(rows_by_file.items()):
        empty_a = sum(1 for r in rows if not r.answer)
        with_ctx = sum(1 for r in rows if r.context)
        with_section = sum(1 for r in rows if r.metadata.get("section"))
        total += len(rows)
        print(f"  {filename[:60]:<60}  rows={len(rows):>4}  "
              f"empty_a={empty_a:>3}  with_ctx={with_ctx:>3}  "
              f"with_section={with_section:>3}")
    print("  " + "─" * 86)
    print(f"  {'TOTAL':<60}  rows={total:>4}")


def print_strategy(name: str, chunks: list[dict],
                   rows: list[Row]) -> None:
    """Aggregate stats + sample chunks (first row and last row)."""
    s = chunk_stats(chunks)
    print(f"\n{name}")
    print("  " + "─" * 86)
    print(f"  chunks: {s['count']}   "
          f"words: avg {s['avg_words']:.1f}  "
          f"min {s['min_words']}  max {s['max_words']}")
    if not chunks or not rows:
        return

    # For Strategy A: chunk i corresponds to row i.
    # For Strategy B: chunks 2i and 2i+1 correspond to row i.
    first_row, last_row = rows[0], rows[-1]
    if "role" in chunks[0]["metadata"]:  # Strategy B
        first_pair = [c for c in chunks if c["metadata"]["pair_id"] == first_row.pair_id]
        last_pair = [c for c in chunks if c["metadata"]["pair_id"] == last_row.pair_id]
        print(f"\n  First row's chunks (pair_id={first_row.pair_id}):")
        for c in first_pair:
            print(f"    [{c['metadata']['role']}]  {_truncate(c['text'])}")
        print(f"\n  Last  row's chunks (pair_id={last_row.pair_id}):")
        for c in last_pair:
            print(f"    [{c['metadata']['role']}]  {_truncate(c['text'])}")
    else:  # Strategy A
        first_chunk = next(
            (c for c in chunks if c["metadata"]["pair_id"] == first_row.pair_id),
            None)
        last_chunk = next(
            (c for c in chunks if c["metadata"]["pair_id"] == last_row.pair_id),
            None)
        print(f"\n  First row's chunk (pair_id={first_row.pair_id}):")
        if first_chunk:
            print(f"    {_truncate(first_chunk['text'])}")
        print(f"\n  Last  row's chunk (pair_id={last_row.pair_id}):")
        if last_chunk:
            print(f"    {_truncate(last_chunk['text'])}")


def prompt_yes_no(message: str) -> bool:
    """Read y/n from stdin. Default-no on empty / ambiguous input."""
    try:
        answer = input(f"\n  {message} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


# ─── Main ───────────────────────────────────────────────────────────────
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview the chunks the ingestion step would produce, "
                    "for both Strategy A (combined) and Strategy B (separated). "
                    "Reads every config_rfi_*.json in the working directory.")
    parser.add_argument(
        "--config-glob",
        default="config_rfi_*.json",
        help="Glob pattern for config files (default: config_rfi_*.json).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_paths = sorted(glob.glob(args.config_glob))
    if not config_paths:
        print(f"ERROR: no configs matched {args.config_glob!r}. Run "
              "profile_excel.py first.", file=sys.stderr)
        return 2

    print("=" * 88)
    print("RFI Chunk Reviewer — preview before ingestion")
    print("=" * 88)
    print(f"\nConfigs found ({len(config_paths)}):")
    for p in config_paths:
        print(f"  - {p}")

    rows_by_file: dict[str, list[Row]] = {}
    all_rows: list[Row] = []
    for cfg_path in config_paths:
        with open(cfg_path) as f:
            cfg = json.load(f)
        xlsx_path = Path("data") / cfg["source_file"]
        try:
            rows = load_excel(str(xlsx_path), cfg)
        except (ValueError, FileNotFoundError) as exc:
            print(f"\nERROR: failed to load {xlsx_path}: {exc}",
                  file=sys.stderr)
            return 3
        rows_by_file[cfg["source_file"]] = rows
        all_rows.extend(rows)

    print_per_file_summary(rows_by_file)

    combined_chunks = build_combined_chunks(all_rows)
    separated_chunks = build_separated_chunks(all_rows)

    print_strategy("Strategy A — Combined (one chunk per row)",
                   combined_chunks, all_rows)
    print_strategy("Strategy B — Separated (two chunks per row, linked by pair_id)",
                   separated_chunks, all_rows)

    print("\n" + "=" * 88)
    print("Summary:")
    print(f"  Total rows across all files:    {len(all_rows)}")
    print(f"  Strategy A chunks (combined):   {len(combined_chunks)}")
    print(f"  Strategy B chunks (separated):  {len(separated_chunks)}")
    print("=" * 88)

    if not prompt_yes_no("Looks good — ready to run ingest_rfi.py?"):
        print("  Not confirmed. Re-run after fixing any issues. "
              "ingest_rfi.py was not invoked from here either way.")
        return 1
    print("  Confirmed. Next step: docker compose run pipeline "
          "python ingest_rfi.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
