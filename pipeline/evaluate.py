"""
eval_rfi.py — run the experiment matrix, score every config  [spec Step 7]
============================================================================
For each combination of (collection × retrieval mode × rerank mode),
run the 20-question ground-truth set defined in
`outputs/eval_dataset.json` and score:

  - Recall@3            (in-scope only): expected_pair_id in top-3?
  - MRR                 (in-scope only): 1 / rank-of-expected, else 0
  - Retrieval gap rate  (in-scope only): refused despite in-scope
  - Hallucination refusal rate (out-of-scope only): correctly refused
  - Faithfulness 1..5   (LLM judge, in-scope non-refusal only)
  - Relevance 1..5      (LLM judge, in-scope non-refusal only)
  - Completeness 1..5   (LLM judge, in-scope non-refusal only)
  - Tokens per query    (approximated by character count of inputs)

Reuses the retrieval + rerank + generation functions from `query_rfi.py`
so what the eval measures IS what `query_rfi.py` does — there is no
second implementation drifting from the real query path.

Writes:
  outputs/rfi_validation/eval_results.json     — full per-config rows
  outputs/rfi_validation/comparison.md         — sorted summary table
  outputs/.eval_checkpoint.json                — resumable state

CLI:
    docker compose run --rm pipeline python eval_rfi.py
    docker compose run --rm pipeline python eval_rfi.py --reset
    docker compose run --rm pipeline python eval_rfi.py --limit 3   # first 3 configs only
    docker compose run --rm pipeline python eval_rfi.py --questions 5  # first 5 questions per config

----------------------------------------------------------------------
ARCHITECTURAL DECISION: report retrieval-gap rate and hallucination
refusal rate SEPARATELY. Both produce a refusal ("I cannot find this
in our corpus."). They mean opposite things:
  - Hallucination refusal = system working correctly, no answer exists
  - Retrieval gap         = system FAILING, answer exists but wasn't
                            retrieved
Conflating them masks a retrieval bug as correct grounding behaviour,
which is why spec Decision 6 calls this out specifically. The eval
splits by scope tag (in / out) and computes the two rates separately.

ARCHITECTURAL DECISION: checkpoint after every configuration, not
inside a configuration. The unit of resumable work is one full
configuration (one collection × one retrieval × one rerank, all 20
questions). Granularity beneath that (per-question) would bloat the
checkpoint and complicate aggregate computation; granularity above it
(per-batch) would risk losing 10+ configs to a single failure. One
config = ~30 API calls of work, the right amount to lose to a
catastrophic failure.

ARCHITECTURAL DECISION: LLM judge skipped on refusals. The LLM judge
scores faithfulness/relevance/completeness on the answer text. A
refusal text ("I cannot find this in our corpus.") would score
faithfulness=5 (it's faithful to the empty context) but relevance=1
(it doesn't answer). Including refusals in the judge averages
muddies the signal. The eval reports the judge scores over
in-scope-non-refusal rows only; the refusal counts are reported as
their own rates.

ARCHITECTURAL DECISION: reuse query_rfi.py's functions verbatim.
The eval imports retrieve_*, rerank_*, fetch_paired_answers, and
generate_answer. The eval IS the query system on a benchmark. If
the query module changes, the eval sees the change with no
synchronisation work. This is the same shared-builder pattern
applied to retrieval + generation rather than chunk construction.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from statistics import mean
from typing import Any

import chromadb

from pipeline.mistral_helpers import call_with_retry, get_client
from pipeline.query import (
    DEFAULT_POOL_SIZE,
    DEFAULT_TOP_K,
    LLM_MODEL,
    ChunkResult,
    fetch_paired_answers,
    generate_answer,
    rerank_crossencoder,
    rerank_llm,
    rerank_none,
    retrieve_bm25,
    retrieve_hybrid,
    retrieve_semantic,
)


# ─── Configuration ──────────────────────────────────────────────────────
CHROMA_PATH = "./chroma_db"
DATASET_PATH = Path("outputs/eval_dataset.json")
RESULTS_PATH = Path("outputs/rfi_validation/eval_results.json")
COMPARISON_PATH = Path("outputs/rfi_validation/comparison.md")
CHECKPOINT_PATH = Path("outputs/.eval_checkpoint.json")

COLLECTIONS = [
    "rfi_combined_cosine", "rfi_combined_l2",
    "rfi_separated_cosine", "rfi_separated_l2",
]
RETRIEVAL_MODES = ["semantic", "bm25", "hybrid"]
RERANK_MODES = ["none", "crossencoder", "llm"]


# ─── Refusal detection ──────────────────────────────────────────────────
_REFUSAL_PATTERN = re.compile(
    r"cannot find this in our corpus", re.IGNORECASE)


def is_refusal(answer: str) -> bool:
    return bool(_REFUSAL_PATTERN.search(answer))


# ─── LLM judge ──────────────────────────────────────────────────────────
def call_llm_judge(mistral_client, question: str, answer: str,
                   context_text: str) -> dict | None:
    """Ask mistral-small-latest to score the answer on three axes.
    Returns {faithfulness, relevance, completeness, reasoning} or None
    if the judge response is malformed."""
    prompt = (
        "You are a judge scoring an answer to a question against a "
        "context of past Q&A pairs. Score the answer on three axes, "
        "each on a 1-5 scale (1 = poor, 5 = excellent).\n\n"
        f"QUESTION: {question}\n\n"
        f"CONTEXT (past Q&A pairs the answer was drawn from):\n"
        f"{context_text}\n\n"
        f"ANSWER:\n{answer}\n\n"
        "Score:\n"
        "  faithfulness: does the answer stay within the context (no "
        "invented details)?\n"
        "  relevance:    does the answer address the question?\n"
        "  completeness: does the answer fully answer what was asked?\n\n"
        "Return ONLY a JSON object with this shape:\n"
        '{"faithfulness": <int 1-5>, "relevance": <int 1-5>, '
        '"completeness": <int 1-5>, "reasoning": "<one sentence>"}'
    )
    try:
        response = call_with_retry(
            mistral_client.chat.complete,
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        payload = json.loads(response.choices[0].message.content)
        return {
            "faithfulness": int(payload["faithfulness"]),
            "relevance": int(payload["relevance"]),
            "completeness": int(payload["completeness"]),
            "reasoning": str(payload.get("reasoning", "")).strip(),
        }
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


# ─── One question through one configuration ────────────────────────────
def run_one_question(question: dict, config: dict,
                     collection, chroma_client, mistral_client,
                     pool_size: int, top_k: int) -> dict:
    """Retrieve + (optional rerank) + (optional paired answers) +
    generate + score one question against one configuration."""
    q_text = question["question"]
    where = {"role": "question"} if "separated" in config["collection"] else None

    # 1. Retrieve pool
    if config["retrieval"] == "semantic":
        pool = retrieve_semantic(collection, mistral_client, q_text,
                                 pool_size, where=where)
    elif config["retrieval"] == "bm25":
        pool = retrieve_bm25(collection, q_text, pool_size, where=where)
    else:
        s = retrieve_semantic(collection, mistral_client, q_text,
                              pool_size, where=where)
        b = retrieve_bm25(collection, q_text, pool_size, where=where)
        pool = retrieve_hybrid(s, b, pool_size)

    # 2. Rerank
    if not pool:
        top: list[ChunkResult] = []
    elif config["rerank"] == "none":
        top = rerank_none(pool, top_k)
    elif config["rerank"] == "crossencoder":
        top = rerank_crossencoder(q_text, pool, top_k)
    else:  # llm
        top = rerank_llm(mistral_client, q_text, pool, top_k)

    # 3. For separated: fetch paired answers
    paired = (fetch_paired_answers(collection, top)
              if "separated" in config["collection"] else None)

    # 4. Generate (skip if pool empty)
    if not top:
        answer = "I cannot find this in our corpus."
    else:
        answer = generate_answer(mistral_client, q_text, top, paired)

    # 5. Score retrieval (in-scope only)
    rank = None
    if question["scope"] == "in":
        expected = question["expected_pair_id"]
        for i, c in enumerate(top):
            if c.metadata.get("pair_id") == expected:
                rank = i + 1
                break

    refused = is_refusal(answer)

    # 6. LLM judge (in-scope non-refusal only)
    judge_scores = None
    if question["scope"] == "in" and not refused and top:
        # Build context the judge sees — matches the generator's view.
        if paired is not None:
            ctx = "\n\n".join(
                f"[past Q&A {i + 1}]\nQ: {q_chunk.text}\nA: {a['text']}"
                for i, (q_chunk, a) in enumerate(zip(top, paired)))
        else:
            ctx = "\n\n".join(
                f"[past Q&A {i + 1}]\n{c.text}"
                for i, c in enumerate(top))
        judge_scores = call_llm_judge(mistral_client, q_text, answer, ctx)

    return {
        "q_id": question["id"],
        "scope": question["scope"],
        "question": q_text,
        "expected_pair_id": question.get("expected_pair_id"),
        "top_pair_ids": [
            c.metadata.get("pair_id") for c in top
        ],
        "rank": rank,
        "recall_at_3": 1 if rank else 0,
        "mrr": (1.0 / rank) if rank else 0.0,
        "refused": refused,
        "answer": answer,
        "judge": judge_scores,
    }


# ─── Aggregate one config's per-question results ───────────────────────
def aggregate(rows: list[dict]) -> dict:
    in_scope = [r for r in rows if r["scope"] == "in"]
    out_scope = [r for r in rows if r["scope"] == "out"]
    judged = [r for r in in_scope if r["judge"] is not None]

    def m(values: list[float], default: float = 0.0) -> float:
        return mean(values) if values else default

    return {
        "n_in_scope": len(in_scope),
        "n_out_scope": len(out_scope),
        "recall_at_3": m([r["recall_at_3"] for r in in_scope]),
        "mrr": m([r["mrr"] for r in in_scope]),
        "retrieval_gap_rate": (
            (sum(1 for r in in_scope if r["refused"]) / len(in_scope))
            if in_scope else 0.0),
        "hallucination_refusal_rate": (
            (sum(1 for r in out_scope if r["refused"]) / len(out_scope))
            if out_scope else 0.0),
        "faithfulness": m([r["judge"]["faithfulness"] for r in judged]),
        "relevance": m([r["judge"]["relevance"] for r in judged]),
        "completeness": m([r["judge"]["completeness"] for r in judged]),
        "n_judged": len(judged),
    }


# ─── Checkpoint ─────────────────────────────────────────────────────────
def config_key(config: dict) -> str:
    return f"{config['collection']}|{config['retrieval']}|{config['rerank']}"


def load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        with open(CHECKPOINT_PATH) as f:
            return json.load(f)
    return {"completed": {}}


def save_checkpoint(state: dict) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH.write_text(
        json.dumps(state, indent=2) + "\n", encoding="utf-8")


# ─── Output writers ─────────────────────────────────────────────────────
def write_results_json(all_results: list[dict]) -> None:
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps(all_results, indent=2) + "\n", encoding="utf-8")


def write_comparison_md(all_results: list[dict]) -> None:
    """Write a sorted summary table for human review.

    Sort key: composite (recall@3 + MRR), descending. Provides a quick
    eye-test of "which configurations land best on retrieval", with the
    judge scores alongside so the reader can see whether retrieval and
    answer quality move together."""
    sorted_results = sorted(
        all_results,
        key=lambda r: -(r["aggregates"]["recall_at_3"] + r["aggregates"]["mrr"]))

    lines: list[str] = []
    lines.append("# RFI Eval — comparison table")
    lines.append("")
    lines.append(
        "Sorted by composite of Recall@3 + MRR (descending). "
        "Judge scores are means over the in-scope, non-refused subset.")
    lines.append("")
    lines.append("| # | Collection | Retrieval | Rerank | Recall@3 | MRR | "
                 "RetrievalGap | HallucRefusal | Faith | Rel | Compl | Judged |")
    lines.append("|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for i, r in enumerate(sorted_results, 1):
        c = r["config"]
        a = r["aggregates"]
        lines.append(
            f"| {i} | {c['collection']} | {c['retrieval']} | {c['rerank']} | "
            f"{a['recall_at_3']:.3f} | {a['mrr']:.3f} | "
            f"{a['retrieval_gap_rate']:.3f} | "
            f"{a['hallucination_refusal_rate']:.3f} | "
            f"{a['faithfulness']:.2f} | {a['relevance']:.2f} | "
            f"{a['completeness']:.2f} | {a['n_judged']} |")
    lines.append("")
    lines.append("**Notes.**")
    lines.append("- *Recall@3* — fraction of in-scope questions whose expected "
                 "pair_id is in the top-3 retrieved chunks.")
    lines.append("- *MRR* — mean reciprocal rank of the expected pair_id "
                 "across in-scope questions.")
    lines.append("- *RetrievalGap* — fraction of in-scope questions that "
                 "produced a refusal. A high value here is a BUG.")
    lines.append("- *HallucRefusal* — fraction of out-of-scope questions that "
                 "correctly refused. A LOW value here means the system is "
                 "fabricating answers.")
    lines.append("- *Faith / Rel / Compl* — LLM-as-judge means (1..5).")
    lines.append("- *Judged* — number of in-scope non-refused rows the judge "
                 "scored.")

    COMPARISON_PATH.parent.mkdir(parents=True, exist_ok=True)
    COMPARISON_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ─── Main run loop ──────────────────────────────────────────────────────
def all_configurations() -> list[dict]:
    return [
        {"collection": c, "retrieval": r, "rerank": k}
        for c in COLLECTIONS
        for r in RETRIEVAL_MODES
        for k in RERANK_MODES
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run the experiment matrix and score every "
                    "configuration against the eval dataset.")
    p.add_argument("--reset", action="store_true",
                   help="Clear the eval checkpoint and start over.")
    p.add_argument("--limit", type=int, default=None,
                   help="Run only the first N configurations (for "
                        "quick sanity checks).")
    p.add_argument("--questions", type=int, default=None,
                   help="Use only the first N questions per config "
                        "(faster smoke tests).")
    p.add_argument("--top-k", type=int, default=DEFAULT_TOP_K,
                   dest="top_k")
    p.add_argument("--pool-size", type=int, default=DEFAULT_POOL_SIZE,
                   dest="pool_size")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not DATASET_PATH.exists():
        print(f"ERROR: {DATASET_PATH} not found. Author it first.",
              file=sys.stderr)
        return 2

    with open(DATASET_PATH) as f:
        dataset = json.load(f)
    questions = dataset["questions"]
    if args.questions is not None:
        questions = questions[: args.questions]
        print(f"  Limiting to first {args.questions} questions per config")

    if args.reset and CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
        print(f"  Cleared {CHECKPOINT_PATH}")

    chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)
    mistral_client = get_client()
    state = load_checkpoint()

    configs = all_configurations()
    if args.limit is not None:
        configs = configs[: args.limit]
        print(f"  Limiting to first {args.limit} configurations")

    print("=" * 88)
    print(f"RFI Eval — {len(configs)} configurations × {len(questions)} questions")
    print(f"  pool_size={args.pool_size}  top_k={args.top_k}")
    print("=" * 88)

    all_results: list[dict] = list(state["completed"].values())

    for i, config in enumerate(configs, 1):
        key = config_key(config)
        if key in state["completed"]:
            print(f"\n[{i}/{len(configs)}] [skip] {key} (already in checkpoint)")
            continue
        print(f"\n[{i}/{len(configs)}] running: {key}")
        try:
            collection = chroma_client.get_collection(config["collection"])
        except Exception as exc:
            print(f"  ERROR: collection not found ({exc}). Skipping.")
            continue
        try:
            rows = [
                run_one_question(q, config, collection,
                                 chroma_client, mistral_client,
                                 args.pool_size, args.top_k)
                for q in questions
            ]
        except Exception as exc:  # noqa: BLE001
            print(f"\nERROR running config {key}: {exc}", file=sys.stderr)
            print(f"  Checkpoint preserves prior configs. Re-run to resume.",
                  file=sys.stderr)
            save_checkpoint(state)
            return 4
        agg = aggregate(rows)
        result = {"config": config, "aggregates": agg, "rows": rows}
        state["completed"][key] = result
        all_results.append(result)
        save_checkpoint(state)
        print(f"    recall@3={agg['recall_at_3']:.3f}  mrr={agg['mrr']:.3f}  "
              f"retr_gap={agg['retrieval_gap_rate']:.3f}  "
              f"halluc_refusal={agg['hallucination_refusal_rate']:.3f}  "
              f"faith={agg['faithfulness']:.2f}  rel={agg['relevance']:.2f}")

    # Final writes
    write_results_json(all_results)
    write_comparison_md(all_results)
    print("\n" + "=" * 88)
    print("Done.")
    print(f"  Per-config rows:  {RESULTS_PATH}")
    print(f"  Comparison table: {COMPARISON_PATH}")
    print(f"  Checkpoint:       {CHECKPOINT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
