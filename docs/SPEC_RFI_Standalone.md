# RFI Answer Builder — Project Spec
**Standalone project. Private repository.**

This document is the single source of truth for the RFI Answer Builder.
It captures every architectural decision, the reasoning behind it, and
the ordered implementation steps for Claude Code.

Work in two modes:
- **Architecture conversation** — decisions and reasoning (captured here)
- **Claude Code** — implementation only, guided by the steps below

---

## Background

This project builds on patterns established in a separate RAG learning
project (CV document Q&A). The following components are copied from that
project and reused here unchanged:

- `models/paragraph.py` — Paragraph dataclass for document parsing
- `loaders/docx_loader.py` — Word document loader
- `loaders/pdf_loader.py` — PDF loader
- `loaders/__init__.py` — format dispatch
- `mistral_helpers.py` — Mistral client + call_with_retry()
- Docker + ChromaDB setup — persistent client, same volume pattern

This project extends that foundation with Excel support, multi-document
ingestion, hybrid retrieval, reranking, and automated evaluation.

---

## Problem statement

A solutions team has answered hundreds of RFI questions across multiple
clients over several years. A new RFI arrives. The question is not "what
does this document say?" — it is "how have we answered questions like this
before, across all our history, and what is the best answer now?"

That is a fundamentally different retrieval problem from single-document Q&A:
- Multiple documents, not one
- Similar questions with different phrasing, not identical ones
- Cross-document reasoning — the best answer may synthesise across RFIs
- Documents that arrive in inconsistent formats — because clients set the format

This project solves the retrieval quality problem for inconsistent
multi-document corpora.

---

## What we're building

A multi-document RFI Q&A system:
- Ingest 3 RFI Excel files with inconsistent schemas
- Profile each document, map columns to roles with human approval
- Store in a shared corpus with rich metadata
- Answer new RFI questions by retrieving the best historical answers
- Experiment with chunking strategy, retrieval method, and reranking

**New concepts introduced:**
- Excel parsing with schema profiling
- Multi-document corpus with metadata-filtered retrieval
- Hybrid retrieval: BM25 keyword + semantic
- Reranking: retrieve more candidates, score by relevance, pass top-n
- Eval framework: automated answer quality scoring
- Two-stage retrieval: RFI corpus + reference document layer

---

## Architecture

```
Excel RFIs (3 files, inconsistent schemas)
      │
      ▼
profile_excel.py ── Mistral ──► column → role mapping recommendation
      │                                    │
human approves                        config_rfi_{n}.json
      │                                    │
      ▼                                    │
review_rfi_chunks.py ──► preview Q&A chunks (no embedding yet)
      │                                    │
human confirms                             │
      │                                    │
      ▼                                    │
ingest_rfi.py ── mistral-embed ──► ChromaDB (rfi_corpus collection)
                                           │
New RFI question
      │
      ▼
query_rfi.py
  ├── semantic search (ChromaDB)      ┐
  ├── BM25 keyword search             ├──► candidate pool
  └── merge + deduplicate            ┘
            │
       reranker
            │
        top-k chunks
            │
      Mistral generation
            │
          Answer
```

---

## Architecture decisions

### Decision 1 — Data model: Row, not Paragraph

The existing `Paragraph` dataclass carries formatting signals (style,
size, bold, numPr) that are meaningless for tabular data. A spreadsheet
row has no paragraph style — it has column values.

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
A `Paragraph` with `text = question + answer` and all formatting fields
set to None is technically possible but semantically wrong. It forces
downstream code to know that `text` is actually a Q&A pair and that all
formatting fields are meaningless. A separate `Row` dataclass is honest
about what it is.

**What this means for the loader pattern:**
`loaders/excel_loader.py` outputs `list[Row]`, not `list[Paragraph]`.
The chunker gets a new path for tabular input. This is a deliberate
deviation from the "one common model" principle — and worth documenting
as a design decision: the common model works when all formats share the
same structural vocabulary (formatting signals). When they don't, a
second model is cleaner than a forced abstraction.

---

### Decision 2 — Excel schema profiler

RFI Excel files have inconsistent schemas because clients set the format.
Column names, positions, and sheet structures vary. The right approach is
to discover structure rather than assume it — the same principle that
drove the document fingerprint profiler in the CV pipeline.

**What the Excel profiler does:**
1. Open each Excel file with `openpyxl`
2. Enumerate sheets — name, row count, column count
3. Sample headers and first 5 rows per sheet
4. Compute per-column statistics: % non-empty, avg word count, sample values
5. Infer likely role per column: question / answer / context / metadata / ignore
6. Flag ambiguity: "column C has short text — could be category tag or notes"
7. Call Mistral with the column profile and ask for role mapping
8. Print recommendation, prompt human approval
9. Write `config_rfi_{filename}.json` on approval

**config_rfi schema:**
```json
{
  "source_file": "rfi_client_a.xlsx",
  "sheet": "Sheet1",
  "columns": {
    "question": "B",
    "answer": "C",
    "context": "D",
    "category": "A",
    "ignore": ["E", "F"]
  },
  "metadata_fields": ["category"],
  "client": "Client A",
  "date": "2024"
}
```

**Why metadata fields matter here:**
`category` (if present) is load-bearing for filtered retrieval — "show me
how we answered security questions" needs a category filter, not full
corpus search. Capturing it at profile time means it travels with every
chunk into ChromaDB.

**Why human approval is load-bearing, not ceremonial:**
The CV pipeline proved that LLMs will sometimes violate explicit schema
constraints despite clear instructions. The human approval gate is the
last defence before a misconfigured schema corrupts the entire corpus.
Treating it as a formality and running with --yes is how silent failures
enter production.

---

### Decision 3 — Chunking strategy experiment

**The core question:**
For a Q&A row, do you embed question + answer together, or separately?

**Option A — Q+A together (one chunk per row):**
```
chunk.text = "Q: What is your data retention policy?\n
              A: All data is retained for 7 years in accordance with..."
chunk.metadata = {source_file, category, client, date, strategy: "combined"}
```

Embedding includes both question and answer semantics. A query matches
against the full Q&A meaning.

Pros: richer embedding, full context in one chunk, simpler retrieval
Cons: query matches against answer text even when the question is what's
relevant; long answers dilute the question signal

**Option B — Q and A separated, linked by metadata:**
```
question_chunk.text = "What is your data retention policy?"
question_chunk.metadata = {pair_id, role: "question", ...}

answer_chunk.text = "All data is retained for 7 years..."
answer_chunk.metadata = {pair_id, role: "answer", ...}
```

Retrieval matches on question similarity only. The paired answer is
fetched by `pair_id` lookup after retrieval — not by embedding similarity.

Pros: query-to-question matching is more precise; answer text doesn't
dilute the retrieval signal
Cons: two-step retrieval (find question, fetch answer); pair_id linkage
must be maintained correctly

**Production recommendation (to be validated by experiment):**
Option B is likely better for RFI use cases because the query IS a
question — matching question-to-question is semantically correct.
Option A is likely better when queries are thematic ("tell me about
security posture") rather than question-shaped.

Both collections built and queried with the same test questions.
Evidence determines the recommendation.

**ChromaDB collections:**
```
rfi_combined_cosine      ← Option A, cosine
rfi_combined_l2          ← Option A, L2
rfi_separated_cosine     ← Option B questions, cosine
rfi_separated_l2         ← Option B questions, L2
```

---

### Decision 4 — Hybrid retrieval: BM25 + semantic

**Why semantic alone is insufficient here:**
Prior work on a CV pipeline found retrieval gaps on short chunks — the
embedding for a 25-word chunk loses cosine similarity contests against
longer chunks. RFI questions are often short and specific. "GDPR
compliance" as a query should retrieve chunks containing those exact
words — semantic search may miss them if the embedding space places
paraphrases closer.

**BM25 (Best Match 25):**
A keyword-based ranking algorithm. Scores documents by term frequency
(how often the query terms appear) weighted by inverse document frequency
(how rare those terms are across the corpus). Fast, exact-match-friendly,
no vectors needed.

BM25 wins when: queries contain specific terminology, acronyms, product
names, or regulatory references that have precise meaning.
Semantic wins when: queries are phrased differently from the document
but mean the same thing.
Hybrid wins when: you don't know which will apply — which is always true
in production.

**Implementation: `rank_bm25` library**
```python
from rank_bm25 import BM25Okapi

# Build index at ingest time
corpus = [chunk.text.split() for chunk in all_chunks]
bm25 = BM25Okapi(corpus)

# At query time
bm25_scores = bm25.get_scores(query.split())
```

**Merging BM25 + semantic results:**
Both return ranked lists. Merge using Reciprocal Rank Fusion (RRF):

```python
def rrf_score(rank, k=60):
    return 1 / (k + rank)

# For each chunk, sum RRF scores from both ranked lists
# Higher combined score = better candidate
```

RRF is simple, parameter-light, and empirically robust. It doesn't
require normalising scores across methods — just ranks.

**Experiment axis:**
- Semantic only (baseline)
- BM25 only (keyword baseline)
- Hybrid RRF (the production approach)

---

### Decision 5 — Reranking

**The problem reranking solves:**
Retrieval (semantic or hybrid) optimises for approximate similarity at
scale. It's fast because it uses vector operations or keyword scoring.
But it's not reading the chunks carefully — it's measuring distance.

A reranker reads each candidate chunk in the context of the query and
scores relevance more precisely. It's slower (an LLM or cross-encoder
call per candidate) so it runs on a small candidate pool, not the full
corpus.

**The pattern:**
```
Query → retrieve top-20 candidates (fast, approximate)
      → rerank top-20 by relevance (slow, precise)
      → pass top-3 to generation
```

**Implementation options:**

*Option A — Mistral reranker (if available):*
Mistral has a reranking endpoint. Keeps everything in one provider.
Check availability at implementation time.

*Option B — Cross-encoder via `sentence-transformers`:*
A small cross-encoder model (e.g. `cross-encoder/ms-marco-MiniLM-L-6-v2`)
runs locally, scores query+chunk pairs, no API cost.
```python
from sentence_transformers import CrossEncoder
model = CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')
scores = model.predict([(query, chunk.text) for chunk in candidates])
```

*Option C — LLM-as-reranker:*
Pass all candidates to Mistral, ask it to rank by relevance. Expensive
but no additional dependency. Good for learning what reranking does
before optimising how it does it.

**Recommendation:** start with Option C (LLM reranker) to understand the
concept, then switch to Option B (cross-encoder) for efficiency. Document
the cost/quality tradeoff between the two.

---

### Decision 6 — Eval framework

**The problem:**
Manual evaluation — read the answer, judge if it's correct — doesn't
scale and isn't reproducible. An eval framework makes quality measurement
automated and comparable across experiment configurations.

**What to measure:**

*Retrieval quality:*
- Recall@k: did the correct chunk appear in the top-k results?
- MRR (Mean Reciprocal Rank): how high did the correct chunk rank?

*Answer quality:*
- Hallucination rate: answers to out-of-scope questions that aren't refusals
- Faithfulness: does the answer stay within the retrieved context?
- Relevance: does the answer address the question?

**Important distinction — hallucination vs retrieval gap:**
Both produce a refusal ("I cannot find this"). They mean opposite things:
- Hallucination refusal = system working correctly, answer not in corpus
- Retrieval gap refusal = system failing, answer IS in corpus but wasn't retrieved

Report these separately. Conflating them makes a retrieval failure look
like correct grounding.

**Implementation: LLM-as-judge**
```python
eval_prompt = """
Given this question: {question}
And this retrieved context: {context}
And this answer: {answer}

Score the answer on:
1. Faithfulness (1-5): does it stay within the provided context?
2. Relevance (1-5): does it address the question?
3. Completeness (1-5): does it fully answer what was asked?

Return JSON only: {"faithfulness": n, "relevance": n, "completeness": n,
"reasoning": "..."}
"""
```

**Why LLM-as-judge is legitimate but imperfect:**
Fast, requires no labelled dataset, correlates reasonably well with human
judgment on factual tasks. Imperfect because the judge can be fooled by
confident-sounding wrong answers. Right starting point — document the
limitation.

**The eval dataset:**
Create a small ground-truth set: 20 questions with known correct answers
from the RFI corpus. Run every experiment configuration against it.
This is the basis for all comparison claims.

---

### Decision 7 — Multi-document metadata strategy

With 3 RFI files in one corpus, metadata is load-bearing — not optional
polish as in a single-document pipeline.

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

**Filtered retrieval examples:**
```python
# Only search security questions
collection.query(
    query_embeddings=[q_embedding],
    where={"category": "Security"},
    n_results=5
)

# Only search a specific client's RFI
collection.query(
    query_embeddings=[q_embedding],
    where={"client": "Client A"},
    n_results=5
)
```

**Tenant isolation note:**
In production, metadata filtering is how multi-tenant RAG enforces access
control — tenant A must never retrieve tenant B's chunks. The pattern
built here is the same pattern. Worth documenting explicitly.

---

## Repository structure

```
rfi-answer-builder/
├── docs/
│   ├── SPEC_RFI.md                ← this document
│   └── LEARNING_NOTES_RFI.md     ← findings, updated during build
├── loaders/
│   ├── __init__.py
│   ├── docx_loader.py             ← copied from CV pipeline
│   ├── pdf_loader.py              ← copied from CV pipeline
│   └── excel_loader.py            ← NEW
├── models/
│   ├── __init__.py
│   ├── paragraph.py               ← copied from CV pipeline
│   └── row.py                     ← NEW
├── mistral_helpers.py             ← copied from CV pipeline
├── profile_excel.py               ← NEW
├── review_rfi_chunks.py           ← NEW
├── ingest_rfi.py                  ← NEW
├── query_rfi.py                   ← NEW
├── eval_rfi.py                    ← NEW
├── Dockerfile                     ← copied from CV pipeline
├── docker-compose.yml             ← copied from CV pipeline
├── requirements.txt               ← copied, extended for new deps
├── .env.example                   ← copied from CV pipeline
├── .gitignore                     ← new, strict
├── data/
│   ├── .gitkeep
│   ├── rfi_1.xlsx                 ← git-ignored
│   ├── rfi_2.xlsx                 ← git-ignored
│   └── rfi_3.xlsx                 ← git-ignored
├── outputs/
│   ├── .gitkeep
│   └── rfi_validation/            ← git-ignored
└── config_rfi_*.json              ← safe to commit, no client data
```

**.gitignore — create this first, before anything else:**
```
# Secrets
.env

# Real RFI data — never commit
data/rfi_*.xlsx
data/*.pdf
data/*.docx

# Outputs may contain RFI content
outputs/rfi_validation/

# Vector store
chroma_db/

# Python
__pycache__/
*.pyc
.pytest_cache/
```

---

## Experiment matrix

All experiments run against the same 20-question eval dataset.

| Axis | Options |
|------|---------|
| Chunk strategy | Combined (A) / Separated (B) |
| Retrieval | Semantic / BM25 / Hybrid RRF |
| Reranking | None / Cross-encoder / LLM-as-judge |
| Distance metric | Cosine / L2 |

**Primary comparison table** (`outputs/rfi_validation/comparison.md`):

| Config | Recall@3 | MRR | Faithfulness | Relevance | Tokens/query |
|--------|----------|-----|-------------|-----------|-------------|
| Combined + Semantic | | | | | |
| Combined + Hybrid | | | | | |
| Separated + Semantic | | | | | |
| Separated + Hybrid | | | | | |
| Separated + Hybrid + Rerank | | | | | |

---

## Implementation steps for Claude Code

Work through these in order. Do not move to the next step until the
current one is verified against known ground truth.

---

### Step 1 — Row dataclass

**Claude Code prompt:**
> Create `models/row.py` with a `Row` dataclass containing fields:
> question (str), answer (str), context (str | None), metadata (dict),
> source_format (str), source_file (str), pair_id (str).
> Add a `__repr__` showing source_file, pair_id, and first 60 chars
> of question. Export from `models/__init__.py`. No logic beyond
> the dataclass.

---

### Step 2 — Excel profiler (`profile_excel.py`)

**Claude Code prompt:**
> Build `profile_excel.py` as an Excel schema profiler.
> Phase 1: open each .xlsx with openpyxl, enumerate sheets, sample
> headers and first 5 rows, compute per-column statistics (% non-empty,
> avg word count, sample values). Infer likely role per column:
> question / answer / context / metadata / ignore — based on word count
> and content patterns.
> Phase 2: call Mistral with the column profile and ask for a role
> mapping. Use call_with_retry() from mistral_helpers.py.
> Phase 3: print recommendation, prompt human approval, write
> config_rfi_{filename}.json on approval.
> Validate every proposed rule before writing config — reject anything
> that doesn't map to a known role.
> CLI: `python profile_excel.py data/rfi_1.xlsx`

---

### Step 3 — Excel loader (`loaders/excel_loader.py`)

**Claude Code prompt:**
> Build `loaders/excel_loader.py` with a single public function:
> `load_excel(path: str, config: dict) -> list[Row]`.
> Read the sheet specified in config, map columns to question/answer/
> context/metadata using config column mappings. Generate a stable
> pair_id for each row: f"{filename}_row_{index}". Skip empty rows.
> Return list[Row]. Add dispatch entry to loaders/__init__.py.
> Test on all three RFI files with their approved configs and confirm
> row counts match expected.

---

### Step 4 — RFI chunk reviewer (`review_rfi_chunks.py`)

**Claude Code prompt:**
> Build `review_rfi_chunks.py` that reads all config_rfi_*.json files,
> loads each Excel file, and prints a preview of proposed chunks for
> both Strategy A (combined) and Strategy B (separated).
> For each strategy show: chunk count, avg word count, min/max,
> sample chunks from first and last rows.
> Print a summary across all three files.
> Prompt for confirmation before proceeding to ingestion.
> CLI: `python review_rfi_chunks.py`

---

### Step 5 — Multi-document ingestion (`ingest_rfi.py`)

**Claude Code prompt:**
> Build `ingest_rfi.py` that ingests all three RFI files into ChromaDB
> using a PersistentClient at ./chroma_db.
> Create 4 collections: rfi_combined_cosine, rfi_combined_l2,
> rfi_separated_cosine, rfi_separated_l2.
> For combined: one chunk per row, text = "Q: {question}\nA: {answer}",
> full metadata attached.
> For separated: two chunks per row (question chunk + answer chunk),
> linked by pair_id in metadata, role field = "question" or "answer".
> Embed using mistral-embed in batches of 16 with call_with_retry().
> Checkpoint progress after each file. Print per-collection summary
> on completion.
> CLI: `python ingest_rfi.py`

---

### Step 6 — Hybrid retrieval + reranking (`query_rfi.py`)

**Claude Code prompt:**
> Build `query_rfi.py` with three retrieval modes selectable via
> --retrieval flag: semantic, bm25, hybrid.
> Semantic: ChromaDB cosine/L2 query.
> BM25: build BM25Okapi index from stored chunk texts at startup,
> score and rank on query.
> Hybrid: run both, merge with Reciprocal Rank Fusion (k=60).
> Add --rerank flag: none, crossencoder, llm.
> crossencoder: use cross-encoder/ms-marco-MiniLM-L-6-v2 to rescore
> top-20 candidates, return top-k.
> llm: pass top-20 candidates to Mistral, ask for relevance ranking,
> return top-k.
> For separated strategy: retrieve question chunks, then fetch paired
> answer chunks by pair_id lookup from metadata.
> Print retrieved chunks with similarity/BM25 scores before the answer.
> CLI: `python query_rfi.py "What is your GDPR compliance approach?"
>   --collection rfi_separated_cosine --retrieval hybrid --rerank
>   crossencoder --top-k 3`

---

### Step 7 — Eval framework (`eval_rfi.py`)

**Claude Code prompt:**
> Build `eval_rfi.py` with a 20-question ground truth dataset defined
> from the RFI corpus (questions with known correct answers — I will
> provide these after ingestion).
> For each experiment configuration in the matrix (chunk strategy x
> retrieval method x reranking), run all 20 questions and score:
> - Hallucination refusal rate (out of scope questions only)
> - Retrieval gap rate (in-scope questions that returned a refusal)
> - Faithfulness (1-5, LLM-as-judge)
> - Relevance (1-5, LLM-as-judge)
> - Recall@3 (did correct chunk appear in top 3?)
> - Tokens per query
> Report hallucination refusal and retrieval gap SEPARATELY.
> Checkpoint after every configuration — one failure must never discard
> prior results.
> Save full results to outputs/rfi_validation/eval_results.json and
> print comparison table to outputs/rfi_validation/comparison.md.
> CLI: `python eval_rfi.py`

---

## Handover notes

This repo is built for internal use and will be handed over on exit.
For whoever picks this up:

**To add a new RFI:**
1. Drop the Excel file in `data/`
2. Run `python profile_excel.py data/your_file.xlsx`
3. Review and approve the column mapping
4. Run `python review_rfi_chunks.py` to preview chunks
5. Run `python ingest_rfi.py` to add to the corpus

**To ask a question:**
```powershell
docker compose run pipeline python query_rfi.py "your question here" \
  --collection rfi_separated_cosine \
  --retrieval hybrid \
  --rerank crossencoder \
  --top-k 3
```

**To run the full evaluation:**
```powershell
docker compose run pipeline python eval_rfi.py
```

**Requirements:**
- Docker Desktop
- Mistral API key in `.env` (see `.env.example`)

---

## Definition of done

- [ ] Excel profiler working on all three RFI files
- [ ] Both chunking strategies ingested into 4 collections
- [ ] Hybrid retrieval implemented and tested
- [ ] Reranking implemented (at least cross-encoder)
- [ ] Eval framework scoring all configurations, hallucination and
      retrieval gaps reported separately
- [ ] Comparison table complete with findings and production recommendation
- [ ] `LEARNING_NOTES_RFI.md` written with findings
- [ ] Handover notes reviewed with CPO

---

## Open questions (parking lot)

- GraphRAG: does the RFI corpus produce enough cross-document relationship
  queries to justify it? Evaluate after eval results are in.
- Reference document layer: if a context document is added, does two-stage
  retrieval outperform single-corpus retrieval?
- Mistral reranker endpoint: check availability at implementation time.
  Compare against cross-encoder if available.
- Scanned PDF support: requires OCR layer, deferred.
