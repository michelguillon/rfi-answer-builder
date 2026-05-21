# RFI Answer Builder — UI Spec
**Web Application Layer on top of the RFI Pipeline**

Standalone document. Extends `SPEC_RFI_Standalone.md`.
The pipeline logic is already built. This spec covers the UI layer only.

---

## What we're adding

A web application that makes the RFI pipeline usable by non-technical
staff without touching the CLI. Two workflows:

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
This tool is designed to sit behind your organisation's existing
authentication layer (reverse proxy, SSO, API gateway). Adding bespoke
auth here would duplicate infrastructure you already have and create
a maintenance burden on handover. To deploy with auth: put Nginx +
your SSO provider in front of the FastAPI service. The session ID
mechanism handles per-user state isolation once a user is admitted.

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
- React + shadcn/ui: production-grade components out of the box
  (file upload, data tables, multi-select, approval dialogs, progress).
  No custom component work needed.
- FastAPI: the pipeline is already Python. FastAPI wraps it with minimal
  friction and has native SSE support.
- SSE over WebSockets: one-directional streaming is all we need for
  the profiler step display. SSE is simpler (no handshake, no library),
  natively supported by FastAPI and the browser's EventSource API.
- Session temp directories over a database: single-purpose internal
  tool, filesystem state is auditable and requires no additional
  infrastructure.

---

## Architecture

```
Browser
  │
  ├── GET /                    React app (served by Vite dev or nginx)
  │
  ├── POST /api/sessions        Create session → session_id
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
  │       {type: "answer",   data: {index, answer, sources, confidence}}
  │       {type: "done"}
  │
  └── GET  /api/answer/export   Download filled Excel
        → streams the file
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
  upload.xlsx          ← uploaded file (ingest or answer workflow)
  profile.json         ← profiler output awaiting approval
  config.json          ← approved config
  answers.json         ← generated answers (answer workflow)
  output.xlsx          ← filled RFI ready for download
```

**Nightly cleanup:**
On startup and at midnight, sweep `./tmp/` and delete any session
directory older than 24 hours. Five lines of code, no cron dependency:

```python
@app.on_event("startup")
async def cleanup_old_sessions():
    cutoff = datetime.now() - timedelta(hours=24)
    for session_dir in Path("./tmp").iterdir():
        if session_dir.stat().st_mtime < cutoff.timestamp():
            shutil.rmtree(session_dir)
```

**Multi-user:** each user gets their own session ID (stored in
localStorage on the frontend). Concurrent users are fully isolated —
no shared state between sessions. Single-user and multi-user behaviour
are identical; the session layer costs nothing for single-user.

---

## Frontend — page structure

### Landing page `/`

Two large cards:

```
┌─────────────────────────┐  ┌─────────────────────────┐
│                         │  │                         │
│   📥 Add RFI to corpus  │  │   📤 Answer a new RFI   │
│                         │  │                         │
│   Upload a past RFI to  │  │   Upload a new client   │
│   improve future        │  │   RFI and get suggested │
│   answers               │  │   answers from our      │
│                         │  │   history               │
│   [Get started →]       │  │   [Get started →]       │
└─────────────────────────┘  └─────────────────────────┘
```

### Workflow 1 — Ingest `/ingest`

**Step 1: Upload**
- shadcn/ui `<Dropzone>` accepting `.xlsx` only
- Shows filename and row count estimate on drop
- [Analyse →] button

**Step 2: Profile (SSE stream)**
- Timeline component — each profiler step appears as it streams
- Steps render as they arrive:
  ```
  ✓ File opened — 169 rows, 6 sheets
  ✓ Sheet selected: "2023 Future Proof Questionnaire" (73 question marks)
  ✓ Header row detected: row 2 (label match: "Question")
  ✓ Columns profiled: A (section), B (question), C (answer)
  ✓ LLM recommendation ready
  ⏳ Awaiting your approval...
  ```
- Proposal card appears at the end:
  ```
  ┌─ Proposed mapping ──────────────────────────┐
  │ Question column:  B                          │
  │ Answer column:    C                          │
  │ Context column:   —                          │
  │ Section column:   A                          │
  │ Client (inferred): Publicis                  │
  │ Date (inferred):   2023                      │
  │                                              │
  │ Sections detected: 10                        │
  │ Q&A rows: 140 (29 section markers stripped)  │
  └──────────────────────────────────────────────┘
  ```
- Editable fields for client and date (user can correct inferences)
- [Approve & Ingest] [Reject & re-profile] buttons

**Step 3: Ingest (SSE stream)**
- Progress bar per collection (4 collections)
- "Embedding batch 3/9..." per collection
- Summary on complete:
  ```
  ✓ rfi_combined_cosine    — 140 chunks
  ✓ rfi_combined_l2        — 140 chunks
  ✓ rfi_separated_cosine   — 268 chunks
  ✓ rfi_separated_l2       — 268 chunks

  Corpus now contains 557 Q&A pairs across 4 RFIs.
  ```
- [Add another RFI] [Go to Answer →] buttons

### Workflow 2 — Answer `/answer`

**Step 1: Upload**
- Same Dropzone as Workflow 1
- Row count preview
- [Extract questions →] button

**Step 2: Question extraction + answering (SSE stream)**
- Progress bar: "Answering question 12 of 47..."
- Each answer streams in as it completes:

  ```
  ┌─ Q12: What is your approach to GDPR compliance? ────────────┐
  │                                                              │
  │ Answer: Our approach to GDPR compliance centres on          │
  │ privacy-by-design principles, documented in our DPIA        │
  │ framework...                                                 │
  │                                                              │
  │ Sources:                                                     │
  │  • Utiq_Publicis_2023 Futureproof — row 42 (score: 0.91)   │
  │  • INTERNAL Reach DPIA — row 17 (score: 0.84)              │
  │  • Guardian OpusVerify — row 29 (score: 0.71)              │
  │                                                              │
  │ [✓ Accept]  [✎ Edit]  [✗ Skip]                             │
  └──────────────────────────────────────────────────────────────┘
  ```

- User can accept, edit inline, or skip each answer as it arrives
- Overall progress visible throughout

**Step 3: Review & export**
- Table of all questions with their status (accepted / edited / skipped)
- Summary: "34 answered, 8 skipped, 5 edited"
- [Download filled RFI] button → exports Excel

**Export format:**
Original Excel columns preserved. Three new columns appended:
```
| ... original columns ... | Suggested Answer | Source RFIs | Confidence |
```
- `Suggested Answer`: accepted or edited answer text
- `Source RFIs`: pipe-separated list of source filenames + row numbers
- `Confidence`: top crossencoder score (0.00–1.00)
- Skipped rows: all three columns left blank

---

## Backend — FastAPI structure

```
api/
  main.py              ← FastAPI app, mounts routers
  routers/
    sessions.py        ← POST /api/sessions
    ingest.py          ← upload, profile (SSE), approve
    answer.py          ← upload, process (SSE), export
  services/
    profiler.py        ← wraps pipeline.profile as async generator
    ingester.py        ← wraps pipeline.ingest as async generator
    answerer.py        ← wraps pipeline.query, iterates questions
    exporter.py        ← builds filled Excel via openpyxl
  session.py           ← session dir management, cleanup
```

**SSE pattern (FastAPI):**

```python
from fastapi.responses import StreamingResponse
import asyncio, json

async def profile_stream(session_id: str):
    async for event in profiler_service.run(session_id):
        yield f"data: {json.dumps(event)}\n\n"

@router.get("/api/ingest/profile")
async def profile_endpoint(session_id: str):
    return StreamingResponse(
        profile_stream(session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache",
                 "X-Accel-Buffering": "no"}
    )
```

**Approval state:**
Between the profiler completing and the human approving, the proposal
lives in `tmp/{session_id}/profile.json`. The POST to
`/api/ingest/approve` reads it, applies any edits from the frontend,
writes `config.json`, and kicks off ingestion. No in-memory state
between requests — filesystem is the state store.

---

## Docker Compose — three services

```yaml
services:
  backend:
    build: .
    volumes:
      - ./chroma_db:/app/chroma_db
      - ./tmp:/app/tmp
      - ./data:/app/data
    environment:
      - MISTRAL_API_KEY=${MISTRAL_API_KEY}
    ports:
      - "8000:8000"

  frontend:
    build: ./frontend
    ports:
      - "3000:3000"
    environment:
      - VITE_API_URL=http://localhost:8000
    depends_on:
      - backend

volumes:
  chroma_db:
  tmp:
```

**`./tmp` is a named volume** — session state survives container
restarts during development. In production the company may want
to bind-mount to a specific path for backup purposes.

---

## Repository structure changes

```
rfi-answer-builder/
├── api/                         ← NEW — FastAPI backend
│   ├── main.py
│   ├── routers/
│   │   ├── sessions.py
│   │   ├── ingest.py
│   │   └── answer.py
│   ├── services/
│   │   ├── profiler.py
│   │   ├── ingester.py
│   │   ├── answerer.py
│   │   └── exporter.py
│   └── session.py
├── frontend/                    ← NEW — React + shadcn/ui
│   ├── src/
│   │   ├── pages/
│   │   │   ├── Landing.tsx
│   │   │   ├── Ingest.tsx
│   │   │   └── Answer.tsx
│   │   ├── components/
│   │   │   ├── StepTimeline.tsx
│   │   │   ├── ProposalCard.tsx
│   │   │   ├── AnswerCard.tsx
│   │   │   └── ExportButton.tsx
│   │   └── lib/
│   │       ├── api.ts           ← typed API calls
│   │       └── sse.ts           ← EventSource wrapper
│   ├── package.json
│   └── Dockerfile
├── loaders/                     ← unchanged
├── models/                      ← unchanged
├── pipeline/                    ← importable package (see pipeline/CLAUDE.md)
│   ├── profile.py               ← wrapped by api/services/profiler.py
│   ├── ingest.py                ← wrapped by api/services/ingester.py
│   ├── query.py                 ← wrapped by api/services/answerer.py
│   ├── evaluate.py
│   ├── review_chunks.py
│   ├── mistral_helpers.py
│   ├── loaders/
│   └── models/
├── docker-compose.yml           ← updated, three services
├── docs/
│   ├── SPEC_RFI_Standalone.md
│   ├── SPEC_UI.md               ← this document
│   └── LEARNING_NOTES_RFI.md
└── tmp/                         ← session state, git-ignored
```

**.gitignore additions:**
```
tmp/
frontend/node_modules/
frontend/dist/
```

---

## Implementation steps for Claude Code

Work through these in order. Each step has a working, testable
deliverable before the next begins.

---

### Step 1 — Backend scaffold + session management

**Claude Code prompt:**
> Set up the FastAPI backend in `api/`. Create `main.py` that mounts
> routers from `api/routers/`. Create `api/session.py` with:
> `create_session() -> str` (creates tmp dir, returns session_id),
> `get_session_dir(session_id) -> Path` (validates exists, raises 404
> if not), `cleanup_old_sessions()` (deletes dirs older than 24hrs).
> Add startup event to main.py that calls cleanup.
> Create stub routers for sessions, ingest, answer — each returns
> a placeholder JSON response.
> Add uvicorn to requirements.txt.
> Verify: `docker compose up backend` starts without errors,
> GET /api/sessions returns placeholder response.

---

### Step 2 — Ingest router: upload + profile SSE

**Claude Code prompt:**
> Implement `api/routers/ingest.py` and `api/services/profiler.py`.
> POST /api/ingest/upload: saves uploaded file to
> tmp/{session_id}/upload.xlsx, returns {session_id, filename,
> detected_rows}.
> GET /api/ingest/profile?session_id=: SSE endpoint. The profiler
> service wraps pipeline.profile as an async generator, yielding
> events as each step completes:
>   {type: "step", data: "message"}  ← each profiler step
>   {type: "proposal", data: {...}}  ← final column mapping proposal
>   {type: "done"}
> Save the proposal to tmp/{session_id}/profile.json before yielding
> the proposal event.
> Use StreamingResponse with media_type="text/event-stream".
> Include X-Accel-Buffering: no header.
> Verify: upload a real RFI Excel, connect to the SSE endpoint,
> confirm all profiler steps stream correctly.

---

### Step 3 — Ingest router: approve + ingest SSE

**Claude Code prompt:**
> Implement POST /api/ingest/approve in `api/routers/ingest.py`
> and `api/services/ingester.py`.
> POST /api/ingest/approve: accepts {session_id, approved_config}
> (user may have edited client/date fields). Writes approved config
> to tmp/{session_id}/config.json. Returns SSE stream of ingest
> progress:
>   {type: "collection", data: "rfi_combined_cosine"}
>   {type: "progress",   data: {batch: 3, total: 9}}
>   {type: "complete",   data: {collection, chunks}}
>   {type: "done",       data: {total_chunks, corpus_size}}
> The ingester service wraps pipeline.ingest.
> Verify: approve a profiled RFI, confirm all 4 collections ingest
> correctly and chunk counts match CLI ingestion.

---

### Step 4 — Answer router: upload + process SSE

**Claude Code prompt:**
> Implement `api/routers/answer.py` and `api/services/answerer.py`.
> POST /api/answer/upload: saves uploaded file to
> tmp/{session_id}/upload.xlsx, extracts questions using the approved
> config pattern (profile lightly — question column only), returns
> {session_id, question_count, questions_preview: first 3}.
> GET /api/answer/process?session_id=: SSE stream. For each question
> in order:
>   {type: "progress", data: {index, total, question_text}}
>   {type: "answer",   data: {index, question, answer, sources,
>                             confidence, pair_ids}}
>   {type: "done"}
> The answerer service calls pipeline.query's retrieve + rerank + generate
> functions. Default config: rfi_separated_cosine + hybrid + crossencoder
> + top-k=3.
> Save all answers to tmp/{session_id}/answers.json on done.
> Verify: upload a real new RFI, confirm all questions are answered
> with correct provenance.

---

### Step 5 — Export service

**Claude Code prompt:**
> Implement `api/services/exporter.py` and
> GET /api/answer/export?session_id= in `api/routers/answer.py`.
> The exporter:
> 1. Opens the original uploaded Excel from tmp/{session_id}/upload.xlsx
> 2. Loads answers from tmp/{session_id}/answers.json
> 3. Appends three columns to the data sheet:
>    "Suggested Answer" — answer text (blank if skipped)
>    "Source RFIs" — pipe-separated source filenames + row numbers
>    "Confidence" — top crossencoder score formatted to 2dp
> 4. Writes to tmp/{session_id}/output.xlsx
> 5. Streams the file as a download response
> Use openpyxl. Preserve all original columns and formatting.
> The export endpoint should also accept a body with per-question
> overrides {index: edited_answer_text} — so frontend edits land
> in the export.
> Verify: export a filled RFI, open in Excel, confirm all three
> columns present and original data intact.

---

### Step 6 — Frontend scaffold

**Claude Code prompt:**
> Create the React frontend in `frontend/` using Vite + TypeScript.
> Install shadcn/ui and initialise it.
> Install required shadcn components: Button, Card, Progress, Badge,
> Textarea, Input, Table, Dialog, Dropzone (or use react-dropzone).
> Create `src/lib/api.ts` with typed fetch wrappers for all API
> endpoints. Create `src/lib/sse.ts` with a typed EventSource wrapper:
> `useSSE(url, onEvent, onDone)` React hook.
> Create stub pages: Landing, Ingest, Answer — each renders a heading
> and a "coming soon" card.
> Set up React Router with routes /, /ingest, /answer.
> Create frontend/Dockerfile: node:20-alpine, npm install, npm run dev.
> Verify: docker compose up → frontend accessible at localhost:3000,
> all three routes render without errors.

---

### Step 7 — Landing page

**Claude Code prompt:**
> Build `src/pages/Landing.tsx`.
> Two large shadcn Cards side by side (stack on mobile).
> Left card: "Add RFI to corpus" with description and arrow button
> linking to /ingest.
> Right card: "Answer a new RFI" with description and arrow button
> linking to /answer.
> Clean, minimal. No auth, no login. The page is the entry point.
> Show corpus stats in a small footer bar: total Q&A pairs in corpus,
> number of source RFIs. Fetch from GET /api/corpus/stats (add this
> lightweight endpoint to the backend — count ChromaDB collection size).

---

### Step 8 — Ingest workflow UI

**Claude Code prompt:**
> Build `src/pages/Ingest.tsx` as a 3-step wizard.
> Step 1 — Upload: react-dropzone accepting .xlsx only. On drop show
> filename. On submit POST to /api/ingest/upload, store session_id
> in localStorage, advance to Step 2.
> Step 2 — Profile: build `src/components/StepTimeline.tsx` that
> renders SSE events as a growing timeline. Each {type:"step"} event
> adds a row with a checkmark. When {type:"proposal"} arrives, render
> `src/components/ProposalCard.tsx` showing the column mapping with
> editable client and date fields. Two buttons: Approve & Ingest
> (POST /api/ingest/approve) and Reject (clears session, back to Step 1).
> Step 3 — Ingest progress: four progress bars (one per collection),
> each advancing on {type:"progress"} events. On {type:"done"} show
> summary with corpus stats. Two buttons: Add another RFI, Go to Answer.

---

### Step 9 — Answer workflow UI

**Claude Code prompt:**
> Build `src/pages/Answer.tsx` as a 3-step flow.
> Step 1 — Upload: same Dropzone as Ingest. On submit POST to
> /api/answer/upload, show question count and 3-question preview.
> [Start answering →] button.
> Step 2 — Processing: overall Progress bar (question X of Y).
> As {type:"answer"} SSE events arrive, render
> `src/components/AnswerCard.tsx` for each:
>   - Question text
>   - Answer text (editable Textarea)
>   - Source list with scores (Badge per source)
>   - Three buttons: Accept (green), Edit (amber, enables textarea),
>     Skip (grey)
> Cards stack as they arrive. User can interact with each immediately
> without waiting for all to complete.
> Step 3 — Review & export: shadcn Table summarising all questions
> with status badges (Accepted / Edited / Skipped). Summary line.
> `src/components/ExportButton.tsx` — on click, POST edits to backend
> then GET /api/answer/export, trigger browser download.

---

## Public repo preparation

Before making the repo public, the UI layer needs a sample dataset
that demonstrates both workflows without real client data.

**`data/sample_rfi_corpus/`** — 2-3 fake RFIs with invented Q&A,
covering the same topic areas (privacy, security, data retention)
without real client names. Pre-ingested so the demo works immediately.

**`data/sample_new_rfi.xlsx`** — a short fake RFI (10 questions) that
can be run through Workflow 2 against the sample corpus.

**Demo script in README:**
```bash
# Load sample corpus
docker compose run backend python -m pipeline.ingest --sample

# Start the app
docker compose up

# Open http://localhost:3000
# Upload sample_new_rfi.xlsx to see Workflow 2 in action
```

---

## Definition of done

- [ ] Backend: all SSE endpoints stream correctly
- [ ] Backend: session cleanup runs on startup
- [ ] Backend: export produces correct Excel with 3 new columns
- [ ] Frontend: both workflows complete end-to-end in the browser
- [ ] Frontend: SSE streams render in real time (no full-page refresh)
- [ ] Frontend: answer cards are editable before export
- [ ] Docker Compose: `docker compose up` starts all three services
- [ ] Sample data created for public demo
- [ ] Auth omission documented in README
- [ ] LEARNING_NOTES_RFI.md updated with UI findings
- [ ] Repo made public with sample data only
