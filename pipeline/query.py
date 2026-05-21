"""
query_rfi.py — retrieve + (optionally) rerank + generate  [spec Step 6]
========================================================================
End-to-end RFI query: given a question and a target collection, retrieve
candidate chunks under one of three retrieval modes, optionally rerank
them, and (for Strategy B collections) fetch the paired answers, then
ask Mistral to draft a final answer using the retrieved context.

Retrieval modes (`--retrieval`):
  semantic   ChromaDB query with the mistral-embed query vector.
  bm25       Keyword scoring (rank_bm25) over all chunk texts.
  hybrid     Run both, merge with Reciprocal Rank Fusion (k=60).
             Hybrid is the production-realistic default — semantic
             alone misses exact-term matches (acronyms, regulatory
             refs); BM25 alone misses paraphrases. Together they
             cover both regimes.

Rerank modes (`--rerank`):
  none           Pass the top-k retrieved chunks straight to generation.
  llm            Send the pool to mistral-small-latest, ask it to
                 re-rank by relevance, take the top-k of the LLM's
                 ranking. One extra API call per query.
  crossencoder   Score every (query, chunk) pair with a small cross-
                 encoder (ms-marco-MiniLM-L-6-v2). Local; no API cost.

Strategy B (separated) collections store question chunks and answer
chunks linked by pair_id. Retrieval matches against question chunks
(query is a question — question-to-question similarity is the right
signal). The paired answers are fetched by id after retrieval and
form the context the generator sees.

CLI:
    python query_rfi.py "What is your GDPR compliance approach?" \\
      --collection rfi_separated_cosine \\
      --retrieval hybrid \\
      --rerank crossencoder \\
      --top-k 3

----------------------------------------------------------------------
ARCHITECTURAL DECISION: retrieve a POOL, then rerank to top-k.
The retrieval modes return `--pool-size` (default 20) candidates.
Rerankers see all 20 and pick the top-k (default 3). This matches
spec Decision 5: fast/approximate retrieval over the full corpus,
slow/precise reranking over a small pool. Reversing the order (pick
top-3 cheaply then rerank) defeats the purpose — the reranker can
only choose among what retrieval surfaced, so it needs a wider
pool to actually improve over retrieval.

ARCHITECTURAL DECISION: BM25 index built fresh per query, not
persisted. Building BM25Okapi from collection.get() takes <100 ms
for our corpus (~280..544 chunks). Caching the index across queries
would save the build cost but introduces stale-index risk after
re-ingest. Build-per-query is the right cost/correctness trade for
this scale; a much larger corpus would want a persistent BM25 store
(Tantivy, Elasticsearch) — out of scope here.

ARCHITECTURAL DECISION: lazy import of sentence-transformers.
The library brings ~600 MB of torch + transformers as transitive
dependencies. A query that doesn't use the crossencoder shouldn't
pay the import latency (~3 s on first load). Importing inside the
rerank function delays the cost until it is actually needed.

ARCHITECTURAL DECISION: Q→A linkage by ID lookup, not metadata WHERE.
Separated chunks were ingested with stable ids `<pair_id>__question`
and `<pair_id>__answer`. After retrieving question chunks, fetching
their paired answers is a single `collection.get(ids=[...])` call —
deterministic, indexed, no metadata filter scan. Cleaner than
`where={"pair_id": ..., "role": "answer"}` which would invoke the
metadata filter machinery per chunk.

ARCHITECTURAL DECISION: final generation reads ALL the retrieved
context. The generator sees the user's question and the top-k
(reranked) Q&A pairs as supporting context, with instructions
that this is "past answers to similar questions" and to refuse if
the context doesn't cover the question. That last clause is the
hallucination guard — without it, Mistral will confabulate when
the corpus has no relevant answer. With it, refusals are
distinguishable from real answers.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any

import chromadb
from rank_bm25 import BM25Okapi

from pipeline.mistral_helpers import call_with_retry, get_client


# ─── Configuration ──────────────────────────────────────────────────────
CHROMA_PATH = "./chroma_db"
EMBED_MODEL = "mistral-embed"
LLM_MODEL = "mistral-small-latest"
RRF_K = 60                 # standard RRF constant; see spec Decision 4
DEFAULT_POOL_SIZE = 20
DEFAULT_TOP_K = 3


# ─── Result shape ───────────────────────────────────────────────────────
@dataclass
class ChunkResult:
    """One retrieved chunk. `score` semantics depend on the source:
        semantic: lower = better (distance)
        bm25:     higher = better (raw BM25)
        rrf:      higher = better (RRF fused)
        crossencoder/llm rerank: higher = better
    The retrieval/rerank layer sorts the list so that index 0 is the
    most relevant under whatever ranking is in force, so downstream
    code doesn't need to know which signal produced the order.
    """
    chunk_id: str
    text: str
    metadata: dict
    score: float
    score_type: str


# ─── Helpers ────────────────────────────────────────────────────────────
def _tokenize(text: str) -> list[str]:
    """Lowercase + alphanumeric token extraction. Same rule applied to
    documents (at index build) and to queries (at search) so BM25
    scoring sees a consistent vocabulary."""
    return re.findall(r"[a-z0-9]+", text.lower())


def _embed_query(mistral_client, query: str) -> list[float]:
    response = call_with_retry(
        mistral_client.embeddings.create,
        model=EMBED_MODEL,
        inputs=[query],
    )
    return response.data[0].embedding


# ─── Retrieval modes ────────────────────────────────────────────────────
def retrieve_semantic(collection, mistral_client, query: str,
                      n: int, where: dict | None = None
                      ) -> list[ChunkResult]:
    """ChromaDB vector query. Score = distance (cosine or L2 per the
    collection's hnsw:space setting). Lower = better.

    `where` filters metadata at the DB level. For separated
    collections we pass `where={"role": "question"}` so the pool
    contains only question chunks — answer chunks are fetched
    afterwards via pair_id linkage (see fetch_paired_answers)."""
    q_vec = _embed_query(mistral_client, query)
    res = collection.query(query_embeddings=[q_vec], n_results=n,
                           where=where)
    out: list[ChunkResult] = []
    for chunk_id, doc, meta, dist in zip(
            res["ids"][0], res["documents"][0],
            res["metadatas"][0], res["distances"][0]):
        out.append(ChunkResult(
            chunk_id=chunk_id, text=doc, metadata=meta or {},
            score=dist, score_type="distance"))
    return out


def _load_all_chunks(collection, where: dict | None = None
                     ) -> tuple[list[str], list[str],
                                list[str], list[dict]]:
    """Pull every chunk from the collection (optionally filtered by
    metadata). Used to build the BM25 index. Returns (ids, texts,
    lowercased-token-lists, metadatas)."""
    all_data = collection.get(where=where) if where else collection.get()
    return (all_data["ids"], all_data["documents"],
            [_tokenize(t) for t in all_data["documents"]],
            all_data["metadatas"] or [{} for _ in all_data["ids"]])


def retrieve_bm25(collection, query: str, n: int,
                  where: dict | None = None) -> list[ChunkResult]:
    """BM25Okapi keyword scoring. Builds the index fresh per query
    (see ARCHITECTURAL DECISION block). Score = BM25 raw, higher = better.

    `where` mirrors retrieve_semantic's filter — for separated
    collections, restrict the corpus to question chunks before
    indexing so answer chunks don't drown them out by length."""
    ids, docs, tokenized, metas = _load_all_chunks(collection, where=where)
    if not ids:
        return []
    bm25 = BM25Okapi(tokenized)
    scores = bm25.get_scores(_tokenize(query))
    # Sort by score descending; take top n.
    indexed = sorted(enumerate(scores), key=lambda t: -t[1])[:n]
    return [
        ChunkResult(
            chunk_id=ids[i], text=docs[i], metadata=metas[i] or {},
            score=float(s), score_type="bm25")
        for i, s in indexed if s > 0  # skip zero-score chunks
    ]


def retrieve_hybrid(semantic: list[ChunkResult], bm25: list[ChunkResult],
                    n: int) -> list[ChunkResult]:
    """Reciprocal Rank Fusion. For each chunk that appears in either
    ranking, sum 1/(RRF_K + rank). Chunks absent from one ranking
    contribute zero from that side. Returns top-n by fused score."""
    semantic_ranks = {c.chunk_id: i + 1 for i, c in enumerate(semantic)}
    bm25_ranks = {c.chunk_id: i + 1 for i, c in enumerate(bm25)}

    all_ids = set(semantic_ranks) | set(bm25_ranks)
    fused: dict[str, float] = {}
    chunks_by_id: dict[str, ChunkResult] = {}
    for c in semantic + bm25:
        chunks_by_id.setdefault(c.chunk_id, c)
    for cid in all_ids:
        s = 0.0
        if cid in semantic_ranks:
            s += 1.0 / (RRF_K + semantic_ranks[cid])
        if cid in bm25_ranks:
            s += 1.0 / (RRF_K + bm25_ranks[cid])
        fused[cid] = s

    ordered = sorted(fused.items(), key=lambda kv: -kv[1])[:n]
    return [
        ChunkResult(
            chunk_id=cid,
            text=chunks_by_id[cid].text,
            metadata=chunks_by_id[cid].metadata,
            score=score,
            score_type="rrf")
        for cid, score in ordered
    ]


# ─── Rerank modes ───────────────────────────────────────────────────────
def rerank_none(candidates: list[ChunkResult], k: int) -> list[ChunkResult]:
    """Take the top-k from the retrieval pool, untouched."""
    return candidates[:k]


def rerank_crossencoder(query: str, candidates: list[ChunkResult],
                        k: int) -> list[ChunkResult]:
    """Cross-encoder rescoring with cross-encoder/ms-marco-MiniLM-L-6-v2.
    Lazy import — only loads torch + transformers when this path runs."""
    from sentence_transformers import CrossEncoder
    model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
    pairs = [(query, c.text) for c in candidates]
    scores = model.predict(pairs)
    rescored = [
        ChunkResult(
            chunk_id=c.chunk_id, text=c.text, metadata=c.metadata,
            score=float(s), score_type="crossencoder")
        for c, s in zip(candidates, scores)
    ]
    rescored.sort(key=lambda c: -c.score)
    return rescored[:k]


def rerank_llm(mistral_client, query: str,
               candidates: list[ChunkResult], k: int) -> list[ChunkResult]:
    """Ask mistral-small-latest to rank the candidates by relevance.
    Returns the top-k from the LLM's ranking. Falls back to the
    untouched order if the LLM's response is malformed."""
    candidate_list = "\n".join(
        f"[{i}] {c.text[:400]}{'…' if len(c.text) > 400 else ''}"
        for i, c in enumerate(candidates)
    )
    prompt = (
        f"You are a relevance reranker. Below is a list of candidate "
        f"text chunks. Rank them by relevance to this question:\n\n"
        f"  QUESTION: {query}\n\n"
        f"  CANDIDATES:\n{candidate_list}\n\n"
        f"Return ONLY a JSON object with one key 'ranking' whose value "
        f"is a list of candidate indices (integers) sorted from most "
        f"relevant to least relevant. Include every index exactly once.\n"
    )
    response = call_with_retry(
        mistral_client.chat.complete,
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    text = response.choices[0].message.content
    try:
        ranking = json.loads(text).get("ranking") or []
        # Validate: every index is in range, no dups.
        seen = set()
        ordered: list[ChunkResult] = []
        for idx in ranking:
            i = int(idx)
            if 0 <= i < len(candidates) and i not in seen:
                ordered.append(candidates[i])
                seen.add(i)
        # Append any indices the LLM missed so we don't drop data.
        for i, c in enumerate(candidates):
            if i not in seen:
                ordered.append(c)
        # Re-tag scores so the display shows "rerank position".
        return [
            ChunkResult(c.chunk_id, c.text, c.metadata,
                        float(len(candidates) - i), "llm_rerank")
            for i, c in enumerate(ordered[:k])
        ]
    except (json.JSONDecodeError, ValueError, KeyError):
        # LLM produced something we can't parse; fall back to original
        # ranking so retrieval+generation still works.
        return candidates[:k]


# ─── Q→A linkage (separated strategy only) ─────────────────────────────
def fetch_paired_answers(collection, question_chunks: list[ChunkResult]
                         ) -> list[dict]:
    """Given a list of question-side chunks (each chunk_id ends with
    '__question'), fetch the paired answer chunks by id lookup.
    Returns aligned list of dicts {text, metadata} for the answers,
    in the same order as the question chunks."""
    answer_ids = [
        c.chunk_id.replace("__question", "__answer")
        for c in question_chunks
    ]
    if not answer_ids:
        return []
    fetched = collection.get(ids=answer_ids)
    # ChromaDB returns ids/docs/metadatas in some order — re-align by id.
    by_id = {
        i: {"text": d, "metadata": m or {}}
        for i, d, m in zip(fetched["ids"], fetched["documents"],
                            fetched["metadatas"] or
                            [{} for _ in fetched["ids"]])
    }
    return [
        by_id.get(aid, {"text": "(answer not found)", "metadata": {}})
        for aid in answer_ids
    ]


# ─── Display ────────────────────────────────────────────────────────────
def _trunc(s: str, n: int = 160) -> str:
    s = s.replace("\n", " | ")
    return s if len(s) <= n else s[: n - 1] + "…"


def print_chunks(label: str, chunks: list[ChunkResult]) -> None:
    print(f"\n{label} ({len(chunks)}):")
    print("  " + "─" * 86)
    for i, c in enumerate(chunks):
        meta_compact = ", ".join(
            f"{k}={v!r}" for k, v in c.metadata.items()
            if k in ("pair_id", "source_file", "client", "section", "role")
        )
        print(f"  [{i + 1}] {c.score_type}={c.score:.4f}  {meta_compact}")
        print(f"      {_trunc(c.text)}")


def print_paired_answers(answers: list[dict]) -> None:
    if not answers:
        return
    print(f"\nPaired answers ({len(answers)}):")
    print("  " + "─" * 86)
    for i, a in enumerate(answers):
        print(f"  [{i + 1}] pair_id={a['metadata'].get('pair_id', '?')}")
        print(f"      {_trunc(a['text'])}")


# ─── Generation ─────────────────────────────────────────────────────────
def build_generation_prompt(query: str,
                            top: list[ChunkResult],
                            paired_answers: list[dict] | None) -> str:
    """Render the user-message for the final answer.

    For combined chunks: each top chunk already contains "Q:...\\nA:..."
    For separated:       top is questions; paired_answers supply the A.
    """
    context_blocks: list[str] = []
    if paired_answers is not None:
        for i, (q_chunk, a) in enumerate(zip(top, paired_answers), 1):
            context_blocks.append(
                f"[past Q&A {i}, source={q_chunk.metadata.get('source_file', '?')}]\n"
                f"Q: {q_chunk.text}\n"
                f"A: {a['text']}"
            )
    else:
        for i, c in enumerate(top, 1):
            context_blocks.append(
                f"[past Q&A {i}, source={c.metadata.get('source_file', '?')}]\n"
                f"{c.text}"
            )
    context = "\n\n".join(context_blocks)
    return (
        "You are drafting an answer to a new RFI question, using past "
        "answers our team has given to similar questions. Use the "
        "supplied past Q&A pairs as the only source of truth. If the "
        "context does not cover the question, say so explicitly — do "
        "not invent details that aren't in the past answers.\n\n"
        f"NEW QUESTION: {query}\n\n"
        f"PAST Q&A PAIRS:\n{context}\n\n"
        "Draft an answer to the new question (a few short paragraphs). "
        "If the past Q&A pairs don't cover the question, reply exactly: "
        "'I cannot find this in our corpus.'"
    )


def generate_answer(mistral_client, query: str,
                    top: list[ChunkResult],
                    paired_answers: list[dict] | None) -> str:
    prompt = build_generation_prompt(query, top, paired_answers)
    response = call_with_retry(
        mistral_client.chat.complete,
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    return response.choices[0].message.content.strip()


# ─── CLI + Main ─────────────────────────────────────────────────────────
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Retrieve + rerank + generate against an RFI ChromaDB "
                    "collection.")
    p.add_argument("question", type=str,
                   help="The question to ask.")
    p.add_argument("--collection", required=True,
                   help="ChromaDB collection name "
                        "(e.g. rfi_separated_cosine).")
    p.add_argument("--retrieval",
                   choices=["semantic", "bm25", "hybrid"],
                   default="hybrid",
                   help="Retrieval mode (default: hybrid).")
    p.add_argument("--rerank",
                   choices=["none", "crossencoder", "llm"],
                   default="none",
                   help="Rerank mode applied to the retrieval pool "
                        "(default: none).")
    p.add_argument("--top-k", type=int, default=DEFAULT_TOP_K,
                   dest="top_k",
                   help=f"Number of chunks passed to generation "
                        f"(default: {DEFAULT_TOP_K}).")
    p.add_argument("--pool-size", type=int, default=DEFAULT_POOL_SIZE,
                   dest="pool_size",
                   help=f"Retrieval candidate pool size before rerank "
                        f"(default: {DEFAULT_POOL_SIZE}).")
    p.add_argument("--no-generate", action="store_true",
                   help="Print retrieved chunks only; skip the final "
                        "Mistral generation call.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    print("=" * 88)
    print(f"RFI Query — collection={args.collection}")
    print(f"  question:  {args.question}")
    print(f"  retrieval: {args.retrieval}   rerank: {args.rerank}   "
          f"top_k: {args.top_k}   pool_size: {args.pool_size}")
    print("=" * 88)

    chroma = chromadb.PersistentClient(path=CHROMA_PATH)
    try:
        collection = chroma.get_collection(args.collection)
    except Exception as exc:
        print(f"\nERROR: collection {args.collection!r} not found "
              f"({exc}). Run ingest_rfi.py first.", file=sys.stderr)
        return 2

    mistral_client = get_client()

    # 1. Retrieve a pool.
    # For separated collections, restrict retrieval to question
    # chunks — answer chunks are fetched via pair_id linkage after
    # reranking. Mixing both in the pool lets the reranker pick
    # answer chunks (which have more text and so more keyword
    # overlap), defeating the question-to-question matching the
    # separated strategy was designed for.
    where = {"role": "question"} if "separated" in args.collection else None

    if args.retrieval == "semantic":
        pool = retrieve_semantic(collection, mistral_client,
                                 args.question, args.pool_size,
                                 where=where)
    elif args.retrieval == "bm25":
        pool = retrieve_bm25(collection, args.question,
                             args.pool_size, where=where)
    else:  # hybrid
        s = retrieve_semantic(collection, mistral_client,
                              args.question, args.pool_size,
                              where=where)
        b = retrieve_bm25(collection, args.question,
                          args.pool_size, where=where)
        pool = retrieve_hybrid(s, b, args.pool_size)

    if not pool:
        print("\nNo retrieval candidates found. Refusing to generate.")
        print("I cannot find this in our corpus.")
        return 0

    print_chunks(f"Retrieval pool ({args.retrieval})", pool)

    # 2. Rerank.
    if args.rerank == "none":
        top = rerank_none(pool, args.top_k)
    elif args.rerank == "crossencoder":
        top = rerank_crossencoder(args.question, pool, args.top_k)
    else:  # llm
        top = rerank_llm(mistral_client, args.question, pool, args.top_k)

    if args.rerank != "none":
        print_chunks(f"After {args.rerank} rerank (top-{args.top_k})", top)
    else:
        print(f"\nTop {args.top_k} (no rerank):")
        for i, c in enumerate(top, 1):
            print(f"  [{i}] {c.metadata.get('pair_id', '?')}")

    # 3. Q→A linkage for separated collections.
    paired = None
    if "separated" in args.collection:
        paired = fetch_paired_answers(collection, top)
        print_paired_answers(paired)

    # 4. Final answer.
    if args.no_generate:
        print("\n(--no-generate: skipping final answer)")
        return 0

    print("\n" + "=" * 88)
    print("ANSWER")
    print("=" * 88)
    answer = generate_answer(mistral_client, args.question, top, paired)
    print(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
