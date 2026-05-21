# pipeline/ — operating notes

This package is the RFI pipeline as importable Python: profiling,
chunking, ingestion, retrieval, generation, evaluation. The root
[CLAUDE.md](../CLAUDE.md) covers cross-cutting conventions (Docker,
Mistral SDK, ChromaDB, code style, branch discipline) — read it
first. This file covers what is specific to the pipeline layer.

## Two contracts, both load-bearing

Every module under `pipeline/` is both:

1. **Independently runnable as a CLI** —
   `docker compose run --rm cli python -m pipeline.profile data/foo.xlsx`.
   `argparse` + `if __name__ == "__main__":` blocks must stay intact;
   the README, [SPEC_RFI_Standalone.md](../docs/SPEC_RFI_Standalone.md),
   and the eval framework all depend on this surface.

2. **Importable from outside the package** — `api/services/profiler.py`
   will do `from pipeline.profile import <fn>`. The module-level
   code path between `import` and the `__main__` guard must be
   side-effect-free, so importers do not trigger CLI behaviour
   (no `argparse.parse_args()`, no `print()` to stdout, no
   `chromadb.PersistentClient(...)` calls at module scope).

Adding a new module to the package? Provide both. Editing an
existing one? Don't break either.

## Behaviour is locked while the UI is being built

The UI layer (`api/` + `frontend/`) wraps these modules without
modifying their behaviour. Organisational moves (rename, split,
relocate, signature tidy-ups) are fine; behavioural changes are
not — they invalidate the production recommendation from
[LEARNING_NOTES_RFI.md](../docs/LEARNING_NOTES_RFI.md) entry 13.

If you find yourself wanting to change behaviour for a UI-driven
reason (different default top-k, different chunking strategy,
softer refusal language, looser hallucination guard), open it as
a separate decision: write a new LEARNING_NOTES entry, propose
alternatives, and confirm with the user. Don't smuggle behavioural
changes through a UI PR.

## Files copied unchanged from a sibling learning project

These were reused as-is from a prior project and should not be
modified casually:

- [loaders/docx_loader.py](loaders/docx_loader.py)
- [loaders/pdf_loader.py](loaders/pdf_loader.py)
- [models/paragraph.py](models/paragraph.py)
- [mistral_helpers.py](mistral_helpers.py)

(Plus `Dockerfile`, `docker-compose.yml`, `requirements.txt`,
`.env.example` at repo root.)

RFI-specific additions in this package — keep these tied to the
LEARNING_NOTES entries that introduced them:

- [loaders/excel_loader.py](loaders/excel_loader.py) — see entry 1
- [models/row.py](models/row.py) — see entry 1
- [profile.py](profile.py), [ingest.py](ingest.py),
  [query.py](query.py), [evaluate.py](evaluate.py),
  [review_chunks.py](review_chunks.py) — see entries 2 onward.

Modifying anything in the "copied unchanged" list is a deliberate
architectural decision and requires a new LEARNING_NOTES entry
explaining what diverged and why.

## Excel-specific conventions

- **openpyxl with `data_only=True`** — load formula *results*, not
  formula strings. Real RFIs ship with formulas in answer cells
  (`=CONCAT(...)`); we want the rendered text. See the docstring
  in [loaders/excel_loader.py](loaders/excel_loader.py).
- **Header detection is intentional, not heuristic.** The profiler
  scans for the first row that contains a 5+ word cell ending in
  `?`. Auto-detect fails on a meaningful fraction of real files;
  the `--header-row` flag is the documented escape hatch. Don't
  silently default to row 1 — surface the ambiguity.

## Resumable checkpoints

`pipeline.ingest` and `pipeline.evaluate` write checkpoints under
`outputs/`:

- `outputs/.ingest_checkpoint.json`
- `outputs/.eval_checkpoint.json`

These let an interrupted run resume without re-embedding or
re-querying. To force a clean restart, use the module's `--reset`
flag — do **not** manually delete the file. The flag also wipes
any stale collection state the checkpoint refers to; manual
deletion leaves you with a half-clean tree (ChromaDB collections
populated, checkpoint absent → next run double-ingests).
