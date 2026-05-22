# RFI Answer Builder

A document-grounded Q&A system for drafting answers to new RFI
(Request For Information) questions from a corpus of past responses.
Built as a learning project to explore production-grade RAG patterns:
schema-aware ingestion, hybrid retrieval, reranking, and evaluation
across a 36-configuration experiment matrix.

## What it does

1. **Profile** an Excel RFI with `mistral-small` to discover its
   column structure (which column has the questions, which the
   answers, etc.) and write a human-approved config.
2. **Ingest** profiled RFIs into ChromaDB across four collections
   (combined-vs-separated chunking × cosine-vs-L2 distance) using
   `mistral-embed`.
3. **Query** the corpus with hybrid retrieval (semantic + BM25),
   optional reranking (crossencoder or LLM-as-reranker), and a
   refusal-guarded generation step that says
   *"I cannot find this in our corpus."* when the corpus is silent.
4. **Evaluate** *(development/learning)* — runs every retrieval ×
   rerank × collection combination against a ground-truth question
   set, with both retrieval metrics (Recall@3, MRR) and LLM-as-judge
   answer scoring, and **separate** reporting of
   hallucination-refusal-rate vs retrieval-gap-rate.

## Known limitations

**Cross-client name leakage.** Generated answers may include past
client names verbatim — the LLM faithfully copies them from source
Q&A pairs. Treat generated answers as draft-for-review, not
send-to-client. The fix path is documented in
`docs/LEARNING_NOTES_RFI.md` entry 14. Until fixed, adding
*"do not name specific clients"* to the generation system prompt
is a partial mitigation.

## Status

| Layer | Status |
|---|---|
| Pipeline (CLI, ChromaDB, eval) | **Complete** — see `docs/SPEC_RFI_Standalone.md` |
| Web UI (FastAPI + React + shadcn/ui) | **Complete** — see `docs/SPEC_UI.md` |

The pipeline is feature-complete with a 36-config production eval
(LEARNING_NOTES entry 13). The UI wraps both workflows (ingest +
answer) so non-technical staff can drive the pipeline without
touching the CLI — full retrieval provenance per answer, plus a
cross-tenant client-mention warning on every generated answer
(LEARNING_NOTES entry 19) and per-RFI delete from the corpus
(entry 25).

## Quick start — web UI

```bash
# One-time setup
cp .env.example .env
# edit .env to add your MISTRAL_API_KEY

# Build images + start backend (FastAPI) + frontend (Vite)
docker compose up backend frontend
# open http://localhost:3000/
```

The UI is two workflows (Add RFI to corpus, Answer a new RFI)
plus per-RFI delete on the Landing page. **Authentication is
deliberately out of scope** — the UI is designed to sit behind
your organisation's existing SSO + reverse proxy. See
`docs/SPEC_UI.md` "What is deliberately out of scope" for the
intended deployment topology.

## Quick start — CLI (pipeline scripts directly)

```bash
# One-time setup
cp .env.example .env

# Build the CLI image (downloads ~1 GB of deps incl. torch
# and sentence-transformers for the crossencoder reranker)
docker compose build cli
```

### Workflow 1 — Add an RFI to the corpus

```bash
# 1. Profile a new RFI. Mistral proposes a column→role mapping;
#    you approve or reject. Writes config_rfi_<slug>.json on approval.
docker compose run --rm cli python -m pipeline.profile data/your_file.xlsx

# 2. Preview the chunks that ingestion would produce, for both
#    chunking strategies. Read-only — does not write to ChromaDB.
docker compose run --rm cli python -m pipeline.review_chunks

# 3. Ingest into ChromaDB across all 4 collections. Resumable via
#    a per-file checkpoint at outputs/.ingest_checkpoint.json.
docker compose run --rm cli python -m pipeline.ingest
```

### Workflow 2 — Query the corpus

```bash
docker compose run --rm cli python -m pipeline.query \
  "What is your approach to GDPR compliance?" \
  --collection rfi_separated_cosine \
  --retrieval hybrid \
  --rerank crossencoder \
  --top-k 3
```

The retrieval pool, reranked top-k, and paired answers are printed
with scores and source attribution **before** the generated answer.
That visibility is deliberate — see `docs/LEARNING_NOTES_RFI.md`
entries 12 and 13 for the reasoning.

Available flags:
- `--retrieval` — `semantic` | `bm25` | `hybrid`
- `--rerank` — `none` | `crossencoder` | `llm`
- `--top-k N` — final chunks passed to generation (default 3)
- `--pool-size N` — candidates retrieved before reranking (default 20)
- `--no-generate` — print retrievals only; skip the Mistral generation

### Run the eval

```bash
docker compose run --rm cli python -m pipeline.evaluate
```

Runs all 36 configurations (4 collections × 3 retrieval modes × 3
rerankers) against the ground-truth question set. Writes:

- `outputs/rfi_validation/eval_results.json` — full per-config rows
- `outputs/rfi_validation/comparison.md` — sorted summary table
- `outputs/.eval_checkpoint.json` — resumable state

Quick smoke test: `--limit 2 --questions 2` runs only 2 configs ×
2 questions.

**Note:** `outputs/eval_dataset.json` is gitignored — it contains
client-identifying pair IDs. To run the eval on your own corpus,
create this file with the schema shown in `pipeline.evaluate`'s
`--help` output.

## Production recommendation

From the eval matrix (see `docs/LEARNING_NOTES_RFI.md` entry 13):

> **`rfi_separated_cosine` + `semantic` + `crossencoder` + top-k=3**

- Recall@3 = 1.0, MRR = 0.971, RetrievalGap = 0.235
- Crossencoder runs locally — no per-query API cost beyond embedding
- All 36 configs achieved 100% hallucination refusal: the system
  never fabricates answers to out-of-scope questions

Counter-intuitive findings worth knowing about (full details in
entry 13):
- Semantic retrieval **beat hybrid** on this corpus — small/paraphrase-
  rich corpora don't benefit much from BM25 fusion
- LLM-as-judge over-scored faithfulness/relevance — actionable signal
  lives in `retrieval_gap_rate` and `completeness`
- Cosine vs L2 was within measurement noise

## Architecture

```
                                Browser
                                   │
                          frontend:3000 (React + Vite + shadcn)
                                   │
                                /api/*
                                   ▼
                          backend:8000 (FastAPI + SSE)
                                   │
                       ┌───────────┴───────────┐
                       │ import (not subprocess)
                       ▼                       ▼
                  pipeline.profile      pipeline.query
                  pipeline.ingest       (retrieve → rerank → generate)
                       │                       │
                       ▼                       ▼
                  ChromaDB (4 collections, embedded PersistentClient)
                  rfi_combined_{cosine,l2}, rfi_separated_{cosine,l2}
```

The same pipeline modules drive both entry points — the CLI runs
them via `python -m pipeline.<module>`, the FastAPI services
import them as Python functions. No subprocess shelling between
layers (see `api/CLAUDE.md`).

## Documentation

| File | Purpose |
|---|---|
| `CLAUDE.md` | Cross-cutting conventions for Claude Code working in this repo (Docker, Mistral SDK, ChromaDB, code style, branch discipline, active memory) |
| `pipeline/CLAUDE.md` | Pipeline-layer rules: dual CLI+import contract, openpyxl conventions, checkpoint discipline |
| `api/CLAUDE.md` | Backend-layer rules: SSE event format, filesystem-backed sessions, "import not subprocess", no-auth-by-design |
| `frontend/CLAUDE.md` | Frontend-layer rules: shadcn-first, useSSE hook contract, verbose provenance mandate, cross-tenant warning placement |
| `docs/SPEC_RFI_Standalone.md` | Pipeline spec — every architectural decision and 7 ordered build steps |
| `docs/SPEC_UI.md` | UI spec — same shape, 9 ordered build steps + 9.5 corpus delete |
| `docs/LEARNING_NOTES_RFI.md` | 26 entries explaining *why* each non-obvious decision was made, alternatives rejected, and the empirical findings from running on real data |

Read the learning notes for the *why*; read the spec for the *what*.

## Privacy

This is a **private repository**. The following are gitignored
AND covered by `.dockerignore` so they cannot land in a built
image either:

```
data/*.xlsx data/*.pdf data/*.docx data/*.csv   (real client RFIs)
config_rfi_*.json                               (column maps include client/date)
outputs/                                        (eval results, checkpoints)
chroma_db/                                      (embedded chunk text)
tmp/                                            (UI per-session state)
.env                                            (Mistral API key)
frontend/node_modules/                          (host-built, platform-specific)
frontend/dist/                                  (built bundle — regenerated in image)
```

The fake `data/sample_rfi.xlsx` is the only explicit exception. If
the repo is ever made public (the UI's "Public repo preparation"
plan in `docs/SPEC_UI.md` covers this), it should ship with sample
data only.

## Stack

- **Python 3.13** in Docker (slim base) — pipeline + FastAPI
- **Mistral** — `mistral-embed` for embeddings, `mistral-small-latest`
  for inference and as the LLM judge / LLM reranker
- **ChromaDB** — `PersistentClient` embedded library, not server
- **openpyxl** — Excel parsing with `data_only=True` (resolved
  formula values, not formula strings)
- **rank_bm25** — keyword scoring; index built fresh per query
- **sentence-transformers** — cross-encoder reranker
  (`cross-encoder/ms-marco-MiniLM-L-6-v2`), lazy-imported
- **FastAPI + uvicorn** — backend (port 8000), SSE-native
- **React 18 + Vite + TypeScript + Tailwind + shadcn/ui** — frontend
  (port 3000)
- **nginx-alpine** — production frontend image only (serves the
  static bundle, proxies `/api/*` to the backend)

## Learning project

This is built as an exploration of production-grade RAG patterns,
not as a polished shippable product. Every architectural decision
has an `ARCHITECTURAL DECISION:` comment block in the relevant
source file **and** a matching entry in
`docs/LEARNING_NOTES_RFI.md` explaining alternatives considered and
the load-bearing reason for the choice. Many of those entries
include empirical findings from running on real data — including
several places where the spec's intuitions turned out to be wrong
(documented in entry 13).
