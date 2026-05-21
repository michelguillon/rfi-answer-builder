# CLAUDE.md — RFI Answer Builder

Operating guide for Claude Code in this repo. Read this first, then
the spec for whichever layer you are working on (see "Current state"
below). The spec for each layer is the single source of truth; this
file captures the conventions that apply across both layers.

---

## Current state

The repo has two layers, each with its own spec:

| Layer | Spec | Status |
|---|---|---|
| **Pipeline** (CLI, ChromaDB, eval) | [docs/SPEC_RFI_Standalone.md](docs/SPEC_RFI_Standalone.md) | **Complete** — all 7 spec steps shipped + 14 learning-note entries + production recommendation. See git log for the per-step commits. |
| **UI** (FastAPI + React + SSE) | [docs/SPEC_UI.md](docs/SPEC_UI.md) | **Next.** 9 implementation steps; should be built on a feature branch off main, not on main directly. |

The pipeline is feature-complete and the eval has landed a production
recommendation (`rfi_separated_cosine` + semantic + crossencoder +
top-k=3). The UI layer wraps the existing pipeline package with a
web app so non-technical staff can run both workflows (ingest +
answer) without touching the CLI. **The pipeline modules
(`pipeline.profile`, `pipeline.ingest`, `pipeline.query`,
`pipeline.evaluate`) keep their existing behaviour — the UI wraps
them by importing the functions, not by editing the modules. Don't
change their behaviour for UI reasons.** (Organisational moves —
renaming, splitting, restructuring — are fine and were done in
commits 1–8 on `feat/ui`.)

---

## What this repo is

A multi-document RFI Q&A system. Ingest Excel RFIs with inconsistent
schemas, profile each, store in a shared corpus with rich metadata,
and answer new RFI questions using hybrid retrieval (BM25 + semantic)
with optional reranking. Now growing a web UI on top.

This is a **private repository**. It will be handed over to the
employer on exit, and a sanitised version may eventually go public
with sample data only (see SPEC_UI.md "Public repo preparation"). No
proprietary client data may ever land in a commit.

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
  [mistral_helpers.py](mistral_helpers.py). It handles 429 + 5xx with
  exponential backoff and honours `Retry-After` headers. 400 / 401 / 404
  are bugs on our side and raise immediately.
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
2. **Mirror in [docs/LEARNING_NOTES_RFI.md](docs/LEARNING_NOTES_RFI.md).**
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
3. Add the corresponding entry to `docs/LEARNING_NOTES_RFI.md`.
4. Stop and confirm with the user before starting the next step.

**Branch discipline.** Pipeline work landed directly on `main`
(commits 35af1cf .. 338c9d3). UI work goes on a feature branch off
main (e.g. `feat/ui`) — don't push to main directly. Merge to main
when the UI's Definition of Done is met.

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

## Files copied unchanged from a sibling learning project

These are reused as-is and should not be modified casually:
- [loaders/docx_loader.py](loaders/docx_loader.py)
- [loaders/pdf_loader.py](loaders/pdf_loader.py)
- [loaders/__init__.py](loaders/__init__.py) (will gain an Excel dispatch entry)
- [models/paragraph.py](models/paragraph.py)
- [models/__init__.py](models/__init__.py) (will gain a `Row` export)
- [mistral_helpers.py](mistral_helpers.py)
- [Dockerfile](Dockerfile), [docker-compose.yml](docker-compose.yml),
  [requirements.txt](requirements.txt), [.env.example](.env.example)

If any of these need to change for RFI-specific reasons, treat it as a
deliberate architectural decision and document it in the learning notes.

---

## Definition of done

**Pipeline (SPEC_RFI_Standalone.md) — DONE.**
- [x] Excel profiler on all 4 real RFI files
- [x] Both chunking strategies ingested into 4 collections (1,646
      chunks total)
- [x] Hybrid retrieval + 3 rerankers implemented
- [x] Eval framework with hallucination/retrieval-gap reported separately
- [x] Comparison table + production recommendation (entry 13)
- [x] LEARNING_NOTES_RFI.md (14 entries)

**UI (SPEC_UI.md) — TODO.** Tracked in that spec's own "Definition
of done". Headline items: SSE endpoints, session cleanup, export
pipeline, both workflows usable in the browser, sample data for
public release, auth omission documented, LEARNING_NOTES updated
with UI-specific findings.
