# RFI Answer Builder — Full Specification
**Standalone + UI layers. Private repository.**

This is the single source of truth for the RFI Answer Builder.
It covers the full system: the CLI pipeline (Phase 1) and the web application layer (Phase 2).

The pipeline spec and the UI spec were originally separate documents. They are combined
here because the two layers share the same persistence model, the same ChromaDB collections,
and the same pipeline functions — the boundary between them is an interface, not an
architectural divide.

Work in two modes:
- **Architecture conversation** — decisions and reasoning (captured here)
- **Claude Code** — implementation only, guided by the steps in each phase

---

## Background

This project builds on patterns established in a separate RAG learning project (CV document Q&A).
The following components are copied from that project and reused here unchanged:

- `models/paragraph.py` — Paragraph dataclass for document parsing
- `loaders/docx_loader.py` — Word document loader
- `loaders/pdf_loader.py` — PDF loader
- `loaders/__init__.py` — format dispatch
- `mistral_helpers.py` — Mistral client + call_with_retry()
- Docker + ChromaDB setup — persistent client, same volume pattern

This project extends that foundation with Excel support, multi-document ingestion,
hybrid retrieval, reranking, automated evaluation, and a production web UI.

---

## Problem statement

A solutions team has answered hundreds of RFI questions across multiple clients over
several years. A new RFI arrives. The question is not "what does this document say?"
— it is "how have we answered questions like this before, across all our history, and
what is the best answer now?"

That is a fundamentally different retrieval problem from single-document Q&A:
- Multiple documents, not one
- Similar questions with different phrasing, not identical ones
- Cross-document reasoning — the best answer may synthesise across RFIs
- Documents that arrive in inconsistent formats — because clients set the format

This project solves the retrieval quality problem for inconsistent multi-document corpora,
and exposes that capability to non-technical staff through a production web application.

---

## What we're building

**Phase 1 — CLI pipeline:**
- Ingest Excel RFI files with inconsistent schemas
- Profile each document, map columns to roles with human approval
- Store in a shared corpus with rich metadata
- Answer new RFI questions by retrieving the best historical answers
- Experiment with chunking strategy, retrieval method, and reranking

**Phase 2 — Web application:**
- Ingest workflow: upload a past RFI → watch the profiler in real time → approve → ingest
- Answer workflow: upload a new client RFI → receive per-question draft answers with
  source provenance → review inline → export as a filled Excel

**New concepts introduced (Phase 1):**
- Excel parsing with schema profiling
- Multi-document corpus with metadata-filtered retrieval
- Hybrid retrieval: BM25 keyword + semantic
- Reranking: retrieve more candidates, score by relevance, pass top-n
- Eval framework: automated answer quality scoring
- Two-stage retrieval: RFI corpus + reference document layer

**New concepts introduced (Phase 2):**
- FastAPI with SSE (Server-Sent Events) for real-time streaming
- Session management via filesystem (no database required)
- React + shadcn/ui production component library
- CLI/UI dual-mode operation via a shared persistence contract

---

# PHASE 1 — CLI PIPELINE

## Architecture

```
Excel RFIs (multiple files, inconsistent schemas)
      │
      ▼
pipeline/profile.py ── Mistral ──► column → role mapping recommendation
      │                                    │
human approves                        config_rfi_{n}.json
      │                                    │
      ▼                                    │
pipeline/review_chunks.py ──► preview Q&A chunks (no embedding yet)
      │                                    │
human confirms                             │
      │                                    │
      ▼                                    │
pipeline/ingest.py ── mistral-embed ──► ChromaDB (4 collections)
                                           │
New RFI question
      │
      ▼
pipeline/query.py
  ├── semantic search (ChromaDB)      ┐
  ├── BM25 keyword search             ├──► candidate pool (top-20)
  └── merge + deduplicate (RRF)      ┘
            │
       reranker (crossencoder / LLM / none)
            │
        top-k chunks
            │
      Mistral generation (with hallucination guard)
            │
          Answer + provenance trace
```

---

## Architecture decisions

### Decision 1 — Data model: Row, not Paragraph

The existing `Paragraph` dataclass carries formatting signals (style, size, bold, numPr)
that are meaningless for tabular data. A spreadsheet row has no paragraph style — it has
column values.

**New dataclass: `Row`**

```python
@dataclass
class Row:
    question: str
    answer: str
    context: str | None        # optional third column
    metadata: dict             # source_file, sheet, row_index,
                               # client, date, category — whatever
                               # the profiler extracted
    source_format: str         # "excel"
    source_file: str           # filename, for cross-doc attribution
    pair_id: str               # stable row identifier for chunk linking
```

**Why not extend `Paragraph`:**
A `Paragraph` with `text = question + answer` and all formatting fields set to None is
technically possible but semantically wrong. It forces downstream code to know that `text`
is actually a Q&A pair and all formatting fields are meaningless. A separate `Row`
dataclass is honest about what it is.

A common intermediate representation is the right abstraction only when the formats it
covers share the same structural vocabulary. The moment a new format has a disjoint
vocabulary (rows and columns vs. paragraphs and styles), a second model is cleaner than
contorting the first.

---

### Decision 2 — Excel schema profiler

RFI Excel files have inconsistent schemas because clients set the format. Column names,
positions, and sheet structures vary. The right approach is to discover structure rather
than assume it.

**What the profiler does:**
1. Open each Excel file with `openpyxl`
2. Select the right sheet using question-mark density (cells ending `?` in first 200 rows)
3. Auto-detect header row via label-match (pass 1) then `?`-content fallback (pass 2)
4. Compute per-column statistics: % non-empty, avg word count, sample values
5. Infer likely role per column: question / answer / context / metadata / ignore
6. Call Mistral with the column profile and ask for role mapping
7. Validate: exactly one question, exactly one answer, all roles in allowed set
8. Print recommendation, prompt human approval
9. Write `config_rfi_{filename}.json` on approval

**Why human approval is load-bearing, not ceremonial:**
The profiler's LLMs occasionally violate explicit schema constraints despite clear
instructions. The human approval gate is the last defence before a misconfigured
schema corrupts the entire corpus. The validator runs BEFORE the human sees the
proposal — the human should only evaluate semantically-correct proposals.

---

### Decision 3 — Chunking strategy experiment

**Option A — Q+A together (one chunk per row):**
```
chunk.text = "Q: What is your data retention policy?\nA: All data is retained for 7 years..."
chunk.metadata = {source_file, category, client, date, strategy: "combined"}
```
Pros: richer embedding, full context in one chunk, simpler retrieval.
Cons: answer text dilutes the question signal; long answers may swamp short questions.

**Option B — Q and A separated, linked by metadata:**
```
question_chunk.text = "What is your data retention policy?"
question_chunk.metadata = {pair_id, role: "question", ...}

answer_chunk.text = "All data is retained for 7 years..."
answer_chunk.metadata = {pair_id, role: "answer", ...}
```
Retrieval matches on question similarity only. The paired answer is fetched by `pair_id`
lookup after retrieval — not by embedding similarity.

**Production finding (from eval):**
Separated wins (R@3=0.980, MRR=0.955 vs 0.961/0.915 for combined), but the margin is
small — 2%/4%. Combined survives as a real alternative if simplicity matters.

**ChromaDB collections:**
```
rfi_combined_cosine      ← Option A, cosine
rfi_combined_l2          ← Option A, L2
rfi_separated_cosine     ← Option B questions, cosine
rfi_separated_l2         ← Option B questions, L2
```

---

### Decision 4 — Hybrid retrieval: BM25 + semantic

**Why semantic alone is insufficient:**
BM25 wins when queries contain specific terminology, acronyms, regulatory references,
or product names. Semantic wins when queries are phrased differently from the document
but mean the same thing.

**Merging via Reciprocal Rank Fusion (RRF):**
```python
def rrf_score(rank, k=60):
    return 1 / (k + rank)
# Sum RRF scores from both ranked lists
```
RRF requires no normalisation across score scales and no training data.

**Production finding (from eval):**
Semantic outperformed hybrid on this corpus. On a small, paraphrase-rich corpus with
natural-language queries, BM25's exact-term advantage doesn't materialise. On a larger
or more terminology-heavy corpus, hybrid would likely outperform.

---

### Decision 5 — Reranking

Retrieval (semantic or hybrid) optimises for approximate similarity at scale. A reranker
reads each candidate chunk in the context of the query and scores relevance more precisely.

**Pattern:**
```
Query → retrieve top-20 candidates (fast, approximate)
      → rerank top-20 by relevance (slow, precise)
      → pass top-3 to generation
```

**Three options implemented:**
- `none` — zero overhead, retrieval ranking is used directly
- `crossencoder` — `cross-encoder/ms-marco-MiniLM-L-6-v2`, local, ~50ms/pair, no API cost
- `llm` — `mistral-small-latest`, one extra API call per query, most capable judge

**Production finding (from eval):**
Crossencoder wins on precision (right chunk at rank 1). LLM rerank produces the lowest
retrieval gap rate and highest completeness — it judges "which chunks will produce an
answerable response," a slightly different criterion. Default is crossencoder (no per-query
API cost); LLM rerank is the upgrade if completeness matters more than cost.

---

### Decision 6 — Eval framework

**What to measure:**

| Metric | What it measures |
|---|---|
| Recall@3 | Did the correct chunk appear in top-3? |
| MRR | How high did the correct chunk rank? |
| hallucination_refusal_rate | Out-of-scope questions that were correctly refused |
| retrieval_gap_rate | In-scope questions that were refused (retrieval FAILURE) |
| Faithfulness (1–5) | Does the answer stay within the retrieved context? |
| Completeness (1–5) | Does the answer fully address the question? |

**Critical distinction — hallucination refusal vs retrieval gap:**
Both produce "I cannot find this in our corpus." They mean opposite things:
- Hallucination refusal = system working correctly, answer not in corpus
- Retrieval gap = system FAILING, answer IS in corpus but wasn't retrieved

Report these separately. Conflating them makes a retrieval failure look like correct
grounding. The LLM judge is skipped on refusals; judge scores cover in-scope,
non-refused answers only.

---

### Decision 7 — Multi-document metadata strategy

With multiple RFI files in one corpus, metadata is load-bearing.

**Required metadata per chunk:**
```python
{
    "source_file": "rfi_client_a.xlsx",
    "client": "Client A",           # from config or filename
    "date": "2024",                 # from config or filename
    "category": "Security",         # from column if present
    "strategy": "combined",         # or "question" / "answer"
    "pair_id": "rfi_a_row_042",    # for Option B chunk linking
    "chunk_index": 42
}
```

**Tenant isolation note:**
In production, metadata filtering is how multi-tenant RAG enforces access control —
tenant A must never retrieve tenant B's chunks. The pattern built here is the same
pattern.

**Cross-tenant content leakage (known issue):**
Metadata filtering handles retrieval isolation. Generation safety is a separate concern:
generated answers can name past clients verbatim from retrieved chunks even when the
retrieval is correctly scoped. Mitigation: prompt-level guard + post-generation name
redaction. Both approaches are documented but the pipeline-layer fix is deferred. The
UI layer surfaces known client-name hits as a warning badge on each answer card.

---

## Repository structure (Phase 1)

```
rfi-answer-builder/
├── pipeline/                    ← importable package
│   ├── __init__.py
│   ├── CLAUDE.md               ← pipeline-specific Claude Code conventions
│   ├── mistral_helpers.py       ← copied from CV pipeline
│   ├── profile.py               ← Excel profiler (was profile_excel.py)
│   ├── review_chunks.py         ← chunk previewer (was review_rfi_chunks.py)
│   ├── ingest.py                ← multi-collection ingester
│   ├── query.py                 ← hybrid retrieval + reranking + generation
│   ├── evaluate.py              ← eval framework
│   ├── loaders/
│   │   ├── __init__.py
│   │   ├── docx_loader.py       ← copied from CV pipeline
│   │   ├── pdf_loader.py        ← copied from CV pipeline
│   │   └── excel_loader.py      ← NEW
│   └── models/
│       ├── __init__.py
│       ├── paragraph.py         ← copied from CV pipeline
│       └── row.py               ← NEW
├── Dockerfile                   ← cli service
├── docker-compose.yml           ← cli + backend + frontend
├── requirements.txt
├── .env.example
├── .gitignore
├── CLAUDE.md                    ← cross-cutting conventions
├── data/
│   ├── .gitkeep
│   └── rfi_*.xlsx               ← git-ignored
├── outputs/
│   ├── .gitkeep
│   └── rfi_validation/          ← git-ignored
└── config_rfi_*.json            ← safe to commit, no client data
```

---

## Experiment matrix

All experiments run against the same 20-question eval dataset (17 in-scope, 3 out-of-scope).

| Axis | Options |
|------|---------|
| Chunk strategy | Combined (A) / Separated (B) |
| Retrieval | Semantic / BM25 / Hybrid RRF |
| Reranking | None / Cross-encoder / LLM-as-judge |
| Distance metric | Cosine / L2 |

**Results summary (36 configurations):**

| Config | Recall@3 | MRR | RetrGap | HallucRefusal | Completeness |
|--------|----------|-----|---------|---------------|--------------|
| rfi_combined_l2 + semantic + llm | 1.000 | 0.931 | 0.176 | 1.000 | 4.86 |
| rfi_separated_l2 + hybrid + none | 1.000 | — | 0.176 | 1.000 | 4.57 |
| rfi_separated_cosine + semantic + crossencoder | 1.000 | 0.971 | 0.235 | 1.000 | 4.62 |

**Production recommendation:**
- Default (cost-sensitive): `rfi_separated_cosine` + `semantic` + `crossencoder` + top-k=3
- Quality-first (when completeness matters): `rfi_combined_l2` + `semantic` + `llm` + top-k=3

---

## Phase 1 implementation steps

### Step 1 — Row dataclass
> Create `pipeline/models/row.py` with a `Row` dataclass containing fields:
> question (str), answer (str), context (str | None), metadata (dict),
> source_format (str), source_file (str), pair_id (str).
> Add a `__repr__` showing source_file, pair_id, and first 60 chars
> of question. Export from `pipeline/models/__init__.py`. No logic beyond the dataclass.

### Step 2 — Excel profiler (`pipeline/profile.py`)
> Build as an Excel schema profiler. Phase 1: open each .xlsx with openpyxl, enumerate
> sheets selecting by question-mark density, auto-detect header row via label-match then
> `?`-content fallback. Phase 2: call Mistral for role mapping. Phase 3: validate
> (exactly one question, exactly one answer) BEFORE showing proposal to human.
> Write `config_rfi_{filename}.json` on approval.
> CLI: `python -m pipeline.profile data/rfi_1.xlsx`

### Step 3 — Excel loader (`pipeline/loaders/excel_loader.py`)
> Build with `load_excel(path, config) -> list[Row]`. Persist header_row from config.
> Skip rows where question is blank. Keep rows where answer is blank (mark them).
> Generate stable pair_id: `{filename_slug}_row_{index}`.
> CLI smoke-test on all files: row counts must match approved configs.

### Step 4 — RFI chunk reviewer (`pipeline/review_chunks.py`)
> Read all `config_rfi_*.json`, load each file, print a preview of both Strategy A
> and Strategy B chunks. Share `build_combined_chunks()` and `build_separated_chunks()`
> functions with the ingester — the reviewer and ingester must build identical chunks.
> Prompt for confirmation before allowing the ingester to proceed.
> CLI: `python -m pipeline.review_chunks`

### Step 5 — Multi-document ingestion (`pipeline/ingest.py`)
> Ingest all RFI files into all four ChromaDB collections.
> Filter empty-text chunks before embedding (Strategy B empty answers).
> Checkpoint per (collection, source_file) pair.
> Use call_with_retry() with batches of 16.
> CLI: `python -m pipeline.ingest`

### Step 6 — Hybrid retrieval + reranking (`pipeline/query.py`)
> Three retrieval modes (--retrieval: semantic, bm25, hybrid).
> Three rerankers (--rerank: none, crossencoder, llm).
> For separated strategy: `where={"role": "question"}` filter at DB level; fetch paired
> answers by `<pair_id>__answer` id lookup after retrieval.
> Hallucination guard in generation prompt: exact refusal sentinel string.
> CLI: `python -m pipeline.query "question here" --collection rfi_separated_cosine
>   --retrieval semantic --rerank crossencoder --top-k 3`

### Step 7 — Eval framework (`pipeline/evaluate.py`)
> Import retrieval + generation functions verbatim from query.py — no second code path.
> 20-question ground-truth dataset with `scope: in|out` per question.
> Track hallucination_refusal_rate and retrieval_gap_rate SEPARATELY.
> Skip LLM judge on refusals.
> Checkpoint per configuration.
> CLI: `python -m pipeline.evaluate`

---

# PHASE 2 — WEB APPLICATION

## What the UI adds

Two workflows for non-technical staff:

**Workflow 1 — Ingest a new RFI into the corpus**
Upload an Excel RFI → watch the profiler analyse it in real time →
review and approve the column mapping → ingest to ChromaDB.

**Workflow 2 — Answer a new RFI using the corpus**
Upload a new client RFI → the pipeline extracts questions and queries
the corpus for each → review answers with provenance → export a filled
Excel with suggested answers, source RFIs, and confidence scores.

---

## What is deliberately out of scope

**Authentication.**
This tool is designed to sit behind your organisation's existing authentication layer
(reverse proxy, SSO, API gateway). Adding bespoke auth here would duplicate
infrastructure you already have and create a maintenance burden on handover.
To deploy with auth: put Nginx + your SSO provider in front of the FastAPI service.
The session ID mechanism handles per-user state isolation once a user is admitted.

---

## Stack

```
React + shadcn/ui    ← frontend (Vite, TypeScript)
FastAPI (Python)     ← backend, wraps existing pipeline
SSE                  ← streams profiler steps to UI in real time
ChromaDB             ← unchanged, persistent volume
Docker Compose       ← three services: frontend, backend, shared volumes
```

**Why this stack:**
- React + shadcn/ui: production-grade components out of the box. No custom component work needed.
- FastAPI: the pipeline is already Python. FastAPI wraps it with minimal friction and has
  native SSE support.
- SSE over WebSockets: one-directional streaming is all we need. SSE is simpler (no
  handshake, no library), natively supported by FastAPI. GET streams could use the
  browser's EventSource API directly, but the POST streams (approve) can't — so the
  frontend reads all SSE over fetch + ReadableStream uniformly (see `src/lib/sse.ts`).
- Session temp directories over a database: single-purpose internal tool, filesystem state
  is auditable and requires no additional infrastructure.

---

## Architecture (API routing table)

```
Browser
  │
  ├── GET /                    React app (served by Vite dev or nginx)
  │
  ├── POST /api/sessions        Create session → session_id
  │
  ├── GET  /api/corpus/stats    Corpus summary (pairs, source files) for landing page
  │
  ├── POST /api/ingest/upload   Upload Excel → save to tmp/{session_id}/
  │
  ├── GET  /api/ingest/profile  SSE stream → profile steps in real time
  │     events:
  │       {type: "step",    data: "Analysing sheet structure..."}
  │       {type: "step",    data: "Detected 3 columns..."}
  │       {type: "proposal",data: {columns, client, date, sections}}
  │       {type: "done"}
  │
  ├── POST /api/ingest/approve  Human approves → writes config, ingests
  │     body: {session_id, approved_config}
  │     SSE stream → ingest progress per file/collection
  │
  ├── POST /api/answer/upload   Upload new RFI → save to tmp/{session_id}/
  │
  ├── GET  /api/answer/process  SSE stream → per-question progress
  │     events:
  │       {type: "question", data: {index, text, total}}
  │       {type: "answer",   data: {index, answer, sources, confidence,
  │                                 refused, mentioned_clients}}
  │       {type: "done"}
  │
  ├── POST /api/answer/edit     Persist per-answer edits + skips → answers.json
  │     body: {session_id, overrides, skipped}
  │
  ├── DELETE /api/corpus/rfi    Remove an RFI from the corpus by source_file
  │
  └── GET  /api/answer/export   Download filled Excel — rebuilt from answers.json
        (which /edit has already updated); streams the file
```

---

## Session management

```python
# On any first request that needs a session
session_id = str(uuid4())
session_dir = Path(f"./tmp/{session_id}")
session_dir.mkdir(parents=True)

# State files per session
tmp/{session_id}/
  upload.xlsx            ← uploaded file (ingest or answer workflow)
  original_filename      ← original filename sidecar (for display)
  profile.json           ← profiler output awaiting approval
  config.json            ← approved config
  answer_questions.json  ← extracted questions + detection method
  answers.json           ← generated answers (answer workflow)
  output.xlsx            ← filled RFI ready for download
```

**Cleanup:** On startup and hourly via asyncio background task, sweep `./tmp/` and delete
any session directory older than 24 hours. The startup sweep guarantees a clean slate on
every restart; the background task handles long-running deployments.

**Multi-user:** each user gets their own session ID (stored in localStorage on the frontend).
Concurrent users are fully isolated — no shared state between sessions.

---

## Cross-client safety

Generated answers can name past clients verbatim from retrieved chunks. The UI
surfaces this:
- Every known client name (from all `config_rfi_*.json` files) is matched against
  the generated answer text using word-boundary regex (`\bNAME\b`, case-insensitive)
- Any hits appear in the answer event's `mentioned_clients` field
- The frontend renders a visible warning badge on the AnswerCard
- This makes the "do not ship send-directly-to-client" rule enforceable by the reviewer

Pipeline-layer fix (prompt guard + post-generation redaction) is documented as a
future improvement.

---

## Frontend — page structure

### Landing page `/`

Two large cards, corpus stats in footer:
```
┌─────────────────────────┐  ┌─────────────────────────┐
│   📥 Add RFI to corpus  │  │   📤 Answer a new RFI   │
│   Upload a past RFI     │  │   Upload a new client   │
│   to improve future     │  │   RFI and get suggested │
│   answers               │  │   answers from our      │
│   [Get started →]       │  │   history               │
└─────────────────────────┘  └─────────────────────────┘
Corpus: 279 Q&A pairs · 4 source RFIs
```

### Workflow 1 — Ingest `/ingest`

**Step 1: Upload** — Dropzone accepting `.xlsx` only.

**Step 2: Profile (SSE)** — Timeline of profiler steps as they stream. Proposal card on
completion with editable client/date fields. [Approve & Ingest] or [Reject] buttons.

**Step 3: Ingest (SSE)** — Progress bar per collection. Summary on complete.

### Workflow 2 — Answer `/answer`

**Step 1: Upload** — Same Dropzone. Question count preview after upload.

**Step 2: Processing (SSE)** — Progress bar + answer cards stream in as each question
completes. Each card shows: question, answer, source list with scores, `mentioned_clients`
warning, and [Accept] [Edit] [Skip] buttons.

**Step 3: Review & export** — Table of all questions with status badges. Export button
sends edits to backend, triggers Excel download.

---

## Backend structure

```
api/
  main.py              ← FastAPI app, mounts routers, lifespan (cleanup + chroma idle thread)
  chroma_client.py     ← lazy-loaded, idle-evicted shared ChromaDB client (see below)
  CLAUDE.md            ← backend-specific Claude Code conventions
  routers/
    sessions.py        ← POST /api/sessions
    corpus.py          ← GET /api/corpus/stats, DELETE /api/corpus/rfi
    ingest.py          ← upload, profile (SSE), approve (SSE)
    answer.py          ← upload, process (SSE), edit, export
  services/
    profiler.py        ← wraps pipeline.profile as async generator
    ingester.py        ← wraps pipeline.ingest as async generator
    answerer.py        ← wraps pipeline.query + cross-client scan
    exporter.py        ← builds filled Excel via openpyxl
  session.py           ← session dir management, cleanup
```

**SSE pattern:**
```python
from fastapi.responses import StreamingResponse
async def profile_stream(session_id: str):
    async for event in profiler_service.run(session_id):
        yield f"data: {json.dumps(event)}\n\n"

@router.get("/api/ingest/profile")
async def profile_endpoint(session_id: str):
    return StreamingResponse(
        profile_stream(session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
```

**Blocking calls:** All synchronous Mistral and ChromaDB calls are wrapped in
`asyncio.to_thread()` so the event loop stays unblocked during concurrent requests.

**Dual persistence:** The approve endpoint writes both `tmp/{sid}/config.json`
(session-local) and `config_rfi_<slug>.json` at repo root (CLI-accessible). Both entry
points (web and CLI) share the same persistent artefacts. No migration needed to add
a third entry point.

---

## ChromaDB lazy loading + idle eviction (API layer)

> **Deviation from the original Phase 2 design, added post-handover.**
> The original spec implied the conventional shape: each service opened a
> `chromadb.PersistentClient` and the process held it for its lifetime. On the
> home-server deployment (entry 26, `restart: unless-stopped`, weeks of uptime)
> that pinned ~1.2GB of RAM indefinitely after the first request, idle or not —
> a compounding cost as more apps share the M720q. The API layer now treats
> ChromaDB as a **reclaimable** resource. Full spec:
> [rfi_CHROMA_LAZY_LOAD_SPEC.md](rfi_CHROMA_LAZY_LOAD_SPEC.md); rationale:
> LEARNING_NOTES entry 28. This is the same "deployment profile broke an
> assumption" upgrade as entry 27's session cleanup.

- **One shared client behind `api/chroma_client.py::get_chroma_client()`.** Every
  API call site (answerer, ingester, corpus stats, corpus delete) goes through it;
  a direct `chromadb.PersistentClient(...)` in `api/` is now banned (api/CLAUDE.md)
  because it opens a second, unmanaged handle the evictor can't reclaim.
- **Cold load on first use** (~5–10s on the real corpus), warm thereafter. A
  `threading.Lock` (not `asyncio.Lock` — the cleanup thread is non-async) guards
  the client; the cold build happens *outside* the lock so concurrent requests
  aren't blocked for the full load.
- **Idle eviction:** a `daemon=True` thread started in `lifespan()` wakes every 60s
  and, after `CHROMA_IDLE_TTL_SECONDS` of inactivity, drops the client + `gc.collect()`.
  Idle footprint returns toward ~50MB (native DuckDB/hnswlib allocators may retain
  some pages — the drop is meaningful, not necessarily complete).
- **`CHROMA_IDLE_TTL_SECONDS`** (default 300) tunes the TTL; `0` disables eviction
  entirely (load once, never release — the thread isn't started).
- **Frontend:** `useSSE` exposes `isSlowLoad`, set after 2s of an open-but-silent
  stream and cleared on the first event (not on `fetch` resolution — Starlette
  flushes SSE headers before the cold load runs). Answer + Ingest show a
  "system is initialising" hint while it's true.

**`pipeline/` is unchanged.** CLI runs are one-shot processes that exit in seconds,
so lazy eviction buys them nothing — they keep creating their own short-lived
`PersistentClient` directly. Lazy loading is an API-layer concern only.

---

## Docker Compose — three services

```yaml
services:
  cli:
    build: .
    volumes:
      - ./chroma_db:/app/chroma_db
      - ./data:/app/data
      - ./outputs:/app/outputs
    environment:
      - MISTRAL_API_KEY=${MISTRAL_API_KEY}
    profiles: [cli]   # only run on explicit 'docker compose run --rm cli'

  backend:
    build: .
    command: uvicorn api.main:app --host 0.0.0.0 --port 8000
    volumes:
      - ./chroma_db:/app/chroma_db
      - ./data:/app/data
      - ./tmp:/app/tmp
    environment:
      - MISTRAL_API_KEY=${MISTRAL_API_KEY}
      - CHROMA_IDLE_TTL_SECONDS=${CHROMA_IDLE_TTL_SECONDS:-300}  # idle eviction; 0 disables
    ports:
      - "8000:8000"
    restart: unless-stopped

  frontend:
    build: ./frontend
    ports:
      - "3000:3000"
    depends_on:
      - backend
    restart: unless-stopped
```

**Production overlay** (`docker-compose.prod.yml`): drops hot-reload, replaces
Vite dev server with nginx serving the built bundle, sets `restart: unless-stopped`.
Data volume is read-write (ingest copies files into `data/` as part of the approve flow).

---

## Repository structure (full, Phase 1 + Phase 2)

```
rfi-answer-builder/
├── pipeline/                    ← importable package (CLI + UI)
│   ├── CLAUDE.md
│   ├── profile.py
│   ├── review_chunks.py
│   ├── ingest.py
│   ├── query.py
│   ├── evaluate.py
│   ├── mistral_helpers.py
│   ├── loaders/
│   └── models/
├── api/                         ← FastAPI backend
│   ├── CLAUDE.md
│   ├── main.py
│   ├── chroma_client.py         ← lazy-loaded, idle-evicted ChromaDB client
│   ├── session.py
│   ├── routers/
│   │   ├── sessions.py
│   │   ├── corpus.py
│   │   ├── ingest.py
│   │   └── answer.py
│   └── services/
│       ├── profiler.py
│       ├── ingester.py
│       ├── answerer.py
│       └── exporter.py
├── frontend/                    ← React + shadcn/ui
│   ├── CLAUDE.md
│   ├── Dockerfile / Dockerfile.prod
│   ├── nginx.conf               ← production frontend server
│   └── src/
│       ├── pages/
│       │   ├── Landing.tsx
│       │   ├── Ingest.tsx
│       │   └── Answer.tsx
│       ├── components/
│       │   ├── StepTimeline.tsx
│       │   ├── ProposalCard.tsx
│       │   ├── AnswerCard.tsx
│       │   └── ExportButton.tsx
│       └── lib/
│           ├── api.ts
│           └── sse.ts
├── CLAUDE.md                    ← cross-cutting conventions
├── docker-compose.yml
├── docker-compose.prod.yml
├── .dockerignore
├── data/                        ← git-ignored (real RFIs)
├── tmp/                         ← git-ignored (session state)
├── chroma_db/                   ← git-ignored (vector store)
└── config_rfi_*.json            ← safe to commit, no client data
```

---

## Phase 2 implementation steps

### Step 1 — Backend scaffold + session management
> Set up `api/` with FastAPI using lifespan context manager (not deprecated
> `@app.on_event`). Implement `api/session.py`: create_session(), get_session_dir()
> (raises 404 if not found), cleanup_old_sessions(). Add startup sweep + hourly
> asyncio background task. Stub routers for sessions, ingest, answer.
> Verify: `docker compose up backend` starts, GET /api/corpus/stats returns placeholder.

### Step 2 — Ingest router: upload + profile SSE
> Implement `api/routers/ingest.py` and `api/services/profiler.py`.
> POST /api/ingest/upload: save file to tmp/{sid}/, persist original_filename sidecar.
> GET /api/ingest/profile SSE: wrap `pipeline.profile` as async generator.
> Save proposal to tmp/{sid}/profile.json before yielding the proposal event.
> Verify: upload a real RFI Excel, confirm all profiler steps stream correctly.

### Step 3 — Ingest router: approve + ingest SSE
> POST /api/ingest/approve: accept {session_id, approved_config} (user may edit
> client/date). Write dual persistence: tmp/{sid}/config.json AND config_rfi_<slug>.json.
> Copy upload to data/<original_filename>. Mark checkpoint. Stream ingest progress.
> Verify: approve a profiled RFI, confirm chunk counts match CLI ingestion.

### Step 4 — Answer router: upload + process SSE
> POST /api/answer/upload: heuristic-first question detection with LLM fallback.
> Return {question_count, questions_preview: first 3, detection_method}.
> GET /api/answer/process SSE: per-question stream with full retrieval trace.
> Scan each answer for known client names; set refused flag on refusal sentinel.
> Default config: rfi_separated_cosine + semantic + crossencoder + top-k=3.
> Save all answers to tmp/{sid}/answers.json on done.

### Step 5 — Export service
> POST /api/answer/edit persists per-question overrides + skips into answers.json;
> GET /api/answer/export then rebuilds the workbook from that file and streams it.
> Appends three columns to original Excel: "Suggested Answer", "Source RFIs", "Confidence".
> Preserves all original columns and formatting (openpyxl, data_only=True).
> Streams file as download response.

### Step 6 — Frontend scaffold
> Vite + TypeScript + shadcn/ui. `src/lib/api.ts` typed fetch wrappers.
> `src/lib/sse.ts` typed SSE hook over fetch + ReadableStream (POST-capable, unlike
> EventSource): `useSSE()` → `{events, status, error, isSlowLoad, start, reset}`.
> Stub pages: Landing, Ingest, Answer. React Router with /, /ingest, /answer.
> Verify: docker compose up → all three routes render without errors.

### Step 7 — Landing page
> Two large Cards. Corpus stats footer fetched from GET /api/corpus/stats.
> Clean, minimal. No auth. This is the entry point.

### Step 8 — Ingest workflow UI
> StepTimeline.tsx renders SSE step events as a growing timeline.
> ProposalCard.tsx shows column mapping with editable client/date fields.
> Four progress bars for ingest (one per collection). Summary on done.

### Step 9 — Answer workflow UI
> Upload → question count + 3-question preview.
> Processing: AnswerCard per answer as it streams, with Accept/Edit/Skip.
> Client-name warning badge on any card with `mentioned_clients` hits.
> Review table with status badges. ExportButton sends edits + triggers download.

### Step 9.5 — Per-RFI delete
> DELETE /api/corpus/rfi: accept {source_file}, remove chunks from all 4 collections,
> delete config_rfi_<slug>.json, remove checkpoint entry, delete data/<file>.
> Surface in the Landing page as a delete button per source RFI in the corpus stats.

---

## Handover notes

**To add a new RFI (CLI):**
```
python -m pipeline.profile data/your_file.xlsx
python -m pipeline.review_chunks
python -m pipeline.ingest
```

**To add a new RFI (UI):** Navigate to /ingest, upload the file, follow the steps.

**To ask a question (CLI):**
```
docker compose run --rm cli python -m pipeline.query "your question here" \
  --collection rfi_separated_cosine --retrieval semantic \
  --rerank crossencoder --top-k 3
```

**To answer a new RFI (UI):** Navigate to /answer, upload the file.

**To run the full evaluation:**
```
docker compose run --rm cli python -m pipeline.evaluate
```

**Requirements:** Docker Desktop, Mistral API key in `.env`.

---

## Definition of done

**Phase 1 (CLI pipeline):**
- [x] Excel profiler working on all RFI files (auto-detect sheet + header row)
- [x] Both chunking strategies ingested into 4 collections
- [x] Hybrid retrieval implemented and tested
- [x] Cross-encoder and LLM reranking implemented
- [x] Eval framework: all 36 configurations scored, hallucination and retrieval gaps separate
- [x] Comparison table complete with production recommendation

**Phase 2 (Web application):**
- [x] All SSE endpoints stream correctly
- [x] Session cleanup: startup sweep + hourly background task
- [x] Export produces correct Excel with 3 new columns
- [x] Both workflows complete end-to-end in the browser
- [x] Answer cards editable before export
- [x] Client-name leakage flagged on answer cards
- [x] Per-RFI delete from corpus
- [x] ChromaDB lazy load + idle eviction in the API layer (reclaimable memory)
- [x] Production deployment: .dockerignore, docker-compose.prod.yml, nginx.conf
- [x] Auth omission documented in README + SPEC + api/CLAUDE.md
- [ ] Sample data for public demo (deferred — requires sanitised repo)

---

## Open questions (parking lot)

- GraphRAG: does the RFI corpus produce enough cross-document relationship queries to
  justify it? Evaluate after more corpus growth.
- Cross-tenant pipeline fix: prompt-level guard + post-generation name redaction.
  Priority increases if the tool is used more widely.
- Reference document layer: if a context document is added, does two-stage retrieval
  outperform single-corpus retrieval?
- ChromaDB server mode: if a second app on the M720q needs vector search, run one
  shared ChromaDB service (HTTP client) instead of two embedded instances. At that
  point the API-layer idle eviction becomes moot — the server is always-on and
  shared. See rfi_CHROMA_LAZY_LOAD_SPEC.md.
- Scanned PDF support: requires OCR layer, deferred.
- LLM-as-judge calibration: explicit anchor rubric or paired-comparison would make
  faithfulness/relevance discriminating rather than ceiling-scored.
