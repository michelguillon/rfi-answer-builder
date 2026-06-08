# CLAUDE.md — RFI Answer Builder

Operating guide for Claude Code in this repo. Read this first, then
the spec for whichever layer you are working on (see "Current state"
below). The spec for each layer is the single source of truth; this
file captures the conventions that apply across both layers.

**Layer-specific conventions** live next to the code they govern
and Claude Code loads them automatically when you work in those
subtrees:

- [pipeline/CLAUDE.md](pipeline/CLAUDE.md) — CLI + import contract,
  Excel/openpyxl rules, checkpoint discipline, "files copied
  unchanged" list.
- [api/CLAUDE.md](api/CLAUDE.md) — SSE event format,
  filesystem-backed sessions, the "import not subprocess" rule
  for wrapping pipeline functions, no-auth-by-design.
- [frontend/CLAUDE.md](frontend/CLAUDE.md) — shadcn-first
  component rules, the `useSSE` hook contract, mandatory
  verbose provenance in AnswerCard, cross-tenant warning
  placement.

---

## Current state

The repo has two layers, both specified in one consolidated document,
[docs/rfi_SPEC.md](docs/rfi_SPEC.md) (Phase 1 = pipeline, Phase 2 = UI):

| Layer | Status |
|---|---|
| **Pipeline** (CLI, ChromaDB, eval) | **Complete** — 7 spec steps + production recommendation. |
| **UI** (FastAPI + React + shadcn/ui) | **Complete** on `feat/ui` — 9 spec steps + delete-RFI (9.5) + polish. |

Both layers are feature-complete. The pipeline's eval (LEARNING_NOTES
entry 13) landed the production recommendation
(`rfi_separated_cosine` + semantic + crossencoder + top-k=3) which
the UI's answer workflow uses as its default config. The UI's full
build is documented in entries 15–25 on the `feat/ui` branch — read
them in order for the rationale, or just read 18 + 19 + 25 for the
load-bearing decisions. **The pipeline modules (`pipeline.profile`,
`pipeline.ingest`, `pipeline.query`, `pipeline.evaluate`) keep their
existing behaviour — the UI wraps them by importing the functions,
not by editing the modules. Don't change their behaviour for UI
reasons.** (Organisational moves — renaming, splitting,
restructuring — are fine and were done in commits 1–8 on `feat/ui`.)

---

## What this repo is

A multi-document RFI Q&A system. Ingest Excel RFIs with inconsistent
schemas, profile each, store in a shared corpus with rich metadata,
and answer new RFI questions using hybrid retrieval (BM25 + semantic)
with optional reranking. Wrapped in a FastAPI + React web UI for
non-technical staff (`/ingest`, `/answer`, plus per-RFI delete on
the Landing page).

This is a **private repository**. It will be handed over to the
employer on exit, and a sanitised version may eventually go public
with sample data only (see rfi_SPEC.md "Public repo preparation"). No
proprietary client data may ever land in a commit OR in a built
Docker image (the `.dockerignore` enforces the latter — keep it
current when adding new data-bearing directories).

---

## Privacy and the .gitignore contract

The `.gitignore` is **load-bearing** — it is the primary defence against
proprietary RFI content leaking into git history. Before touching anything
that creates files, confirm the path is covered:

- `data/rfi_*.xlsx`  — real RFI files (the fake `data/sample_rfi.xlsx` is
  the only exception, explicitly un-ignored)
- `outputs/`         — eval results, retrieval traces, validation runs
- `config_rfi_*.json` — column mappings carry client/date strings
- `chroma_db/`       — vector store embeds chunk text
- `.env`             — Mistral API key

If you find yourself about to write a file outside these patterns that
contains question/answer text, **stop and ask**. Do not improvise.

This repo must never share git history with any sibling project. If a
merge, rebase, or remote-add command would pull in another repo's
history, refuse and surface the request to the user.

---

## How everything runs: Docker, not venv

There is no Python virtual environment. Every pipeline module runs
inside the `cli` container defined in [docker-compose.yml](docker-compose.yml):

```powershell
docker compose run --rm cli python -m pipeline.<module> [args]
```

(The service is named `cli`, not `pipeline`, to avoid colliding
with the Python package of the same name.)

The whole project directory is bind-mounted onto `/app`, so:
- edits on the host take effect immediately (no rebuild)
- state written by one run (`config_rfi_*.json`, `chroma_db/`,
  `outputs/`) survives the throwaway container and is visible to the next
- `.env` is loaded automatically by Compose; `MISTRAL_API_KEY` is the
  only required variable

To verify a Python import works, use:
```powershell
docker compose run --rm cli python -c "from pipeline.models import Row; print(Row)"
```

---

## Mistral SDK conventions

- **Import path:** `from mistralai.client import Mistral`. NOT
  `from mistralai import Mistral` — the v2 SDK exposes the client under
  `mistralai.client`.
- **Every API call goes through `call_with_retry()`** from
  [pipeline/mistral_helpers.py](pipeline/mistral_helpers.py). It
  handles 429 + 5xx with exponential backoff and honours
  `Retry-After` headers. 400 / 401 / 404 are bugs on our side and
  raise immediately.
- **Embedding model is fixed:** `mistral-embed`, 1024 dimensions,
  L2-normalised. Same model for documents and queries. Changing it means
  a full re-index of every collection.

---

## ChromaDB conventions

- Always use `PersistentClient(path="./chroma_db")`. Never the HTTP client
  — we run the embedded library, not a server (see docker-compose.yml).
- Distance metric is set at **collection creation** and is **immutable**.
  This is why the experiment matrix creates separate `_cosine` and `_l2`
  collections rather than trying to switch metrics at query time.
- Collection names from the spec:
  `rfi_combined_cosine`, `rfi_combined_l2`,
  `rfi_separated_cosine`, `rfi_separated_l2`.
- Metadata is **load-bearing** in this project (multi-document corpus):
  `source_file`, `client`, `date`, `category`, `strategy`, `pair_id`,
  `chunk_index`. Filtered retrieval (`where={...}`) depends on these.

---

## Code style — this is a learning project

The reader is a CPO building solution-architect intuition, not a junior
developer learning Python syntax. Calibrate comments accordingly:

1. **`ARCHITECTURAL DECISION:` blocks.** Every non-obvious choice gets
   one — what was chosen, what was rejected, and why. Place at the top of
   the module or immediately above the construct it explains.
2. **Mirror in [docs/rfi_LEARNING_NOTES.md](docs/rfi_LEARNING_NOTES.md).**
   Each `ARCHITECTURAL DECISION:` block should have a short companion
   entry in the learning notes: the decision, alternatives rejected, and
   why. The code explains the implementation; the notes explain the
   intuition. Both, not one.
3. **Explain at solution-architect level**, not developer level. "We use
   a dataclass" is developer level; "we use a separate dataclass because
   the structural vocabulary of tabular data is disjoint from formatting
   signals" is solution-architect level. Write the second.
4. **No comments for the obvious.** Well-named identifiers carry the
   what. Comments carry the why.

---

## Naming traps to avoid

**Never name a script after a Python stdlib module.** Python places the
script directory first on `sys.path`, so a local `inspect.py`,
`types.py`, `json.py`, `csv.py`, `tokenize.py`, `email.py`, etc. shadows
the stdlib for every other script in the project. If you need a name
that collides, prefix it: `rfi_inspect.py`, not `inspect.py`.

---

## Step-by-step workflow

Whichever spec is active: the spec lists ordered implementation steps
(7 for the pipeline, 9 for the UI). **Work through them in order. Do
not move to the next step until the current one is verified.** Each
step has a Claude Code prompt embedded in the spec — treat that as
the contract for what to build.

After each step:
1. Verify the artifact works (import test, CLI smoke test, browser
   test for UI work, or whatever the step's contract demands).
2. Write the `ARCHITECTURAL DECISION:` block(s) into the code.
3. Add the corresponding entry to `docs/rfi_LEARNING_NOTES.md`.
4. Stop and confirm with the user before starting the next step.

**Branch discipline.** Pipeline work landed directly on `main`
(commits 35af1cf .. 338c9d3). The UI phase — including the
pre-UI restructure — landed on `feat/ui` and is ready to merge.
Future work continues on feature branches off main; don't push
to main directly.

---

## Active memory

Two pieces of guidance from earlier work in this repo live in the
project's memory directory and apply to anything that touches the
retrieval/generation path:

- **Verbose provenance is the default for RAG outputs.** Show source
  + pair_id + ranking scores alongside the generated answer; don't
  hide the retrieval trace. The CPO's positive feedback specifically
  cited this. UI answer cards should preserve this visibility.
- **Cross-tenant content leakage is a known unfixed issue.**
  Generated answers can include past client names verbatim because
  the source corpus does. LEARNING_NOTES entry 14 lists the design
  options (prompt guard + post-redaction). When building the UI's
  answer workflow, at minimum surface this risk to the human reviewer
  — e.g. flag answers that mention a client name other than the
  current target client. Don't ship a "send directly to client" path
  that bypasses human review.

---

## Definition of done

**Pipeline (rfi_SPEC.md) — DONE.**
- [x] Excel profiler on all 4 real RFI files
- [x] Both chunking strategies ingested into 4 collections
- [x] Hybrid retrieval + 3 rerankers implemented
- [x] Eval framework with hallucination/retrieval-gap reported separately
- [x] Comparison table + production recommendation (entry 13)
- [x] rfi_LEARNING_NOTES.md entries 1–14

**UI (rfi_SPEC.md) — DONE on `feat/ui`.**
- [x] Backend: all SSE endpoints stream correctly
- [x] Backend: session cleanup runs on startup
- [x] Backend: export produces correct Excel with 3 new columns
- [x] Frontend: both workflows complete end-to-end in the browser
- [x] Frontend: SSE streams render in real time
- [x] Frontend: answer cards are editable + show full provenance
- [x] Frontend: cross-tenant client warning surfaced per answer
- [x] Frontend: per-RFI delete from corpus on Landing page (Step 9.5)
- [x] Docker Compose: `docker compose up` starts backend + frontend
- [x] Auth omission documented in README + rfi_SPEC + api/CLAUDE.md
- [x] rfi_LEARNING_NOTES.md entries 15–25
- [ ] Sample data created for public demo (deferred — only relevant
      when preparing the eventual public-release sanitised repo)
