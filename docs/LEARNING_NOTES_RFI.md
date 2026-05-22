# Learning Notes — RFI Answer Builder

The companion file to [SPEC_RFI_Standalone.md](SPEC_RFI_Standalone.md). The
spec captures *what* the system does and *how* it is built. This file
captures *why* — the intuition behind each architectural decision, the
alternatives that were considered and rejected, and the findings that
emerge once code meets reality.

The code carries `ARCHITECTURAL DECISION:` comment blocks that explain
the choice in situ. Every such block has a matching entry here that
explains the choice as a piece of solution-architect intuition: what
trade-off was being navigated, what would have gone wrong with the
rejected option, and what the choice teaches that generalises beyond
this project.

---

## How to read this file

Entries are append-only and ordered by implementation step. Each entry is
short — a few paragraphs at most — and stands on its own. The format:

```
## N. Short decision title — file or component it lives in

**Context.** One paragraph: what problem was being solved.

**Options considered.** Bullets: the realistic alternatives.

**Choice and reason.** What was picked and the load-bearing reason.

**What it teaches.** One sentence — the generalisable lesson.
```

If a later step changes an earlier decision, write a new entry that
references the old one rather than editing the old one in place. The
audit trail is the point.

---

## Entries

## 1. Row is a separate dataclass from Paragraph — `models/row.py`

**Context.** The pipeline already has a common intermediate model
(`Paragraph`) that every prose loader (docx, pdf) converts its native
output into, so downstream code does not branch on source format. The
RFI pipeline adds an Excel loader. The first question of the build is
whether Excel rows should be coerced into `Paragraph` (preserving the
"one common model" invariant), or whether they should get their own
dataclass `Row`.

**Options considered.**

- *Reuse `Paragraph`.* Set `text = f"Q: {question}\nA: {answer}"`, leave
  `style_name`, `rendered_size`, `is_bold`, `has_num_pr`, `in_table` as
  None / False, smuggle question/answer/context/metadata onto the
  paragraph via ad-hoc attributes or a side-channel dict.
- *Subclass `Paragraph`.* Add `class QARow(Paragraph)` with extra
  fields for question/answer/context/metadata/pair_id. Single root
  type, polymorphic downstream.
- *New peer dataclass `Row`* alongside `Paragraph`. Two models, both
  exported from `models`; the chunker dispatches on type.

**Choice and reason.** New peer dataclass. The deciding factor is
*structural vocabulary*: `Paragraph`'s fields exist to discriminate
headings from body text from list items in a flowing document — style,
font size, bold, list status, table membership. A spreadsheet row has
none of that vocabulary; it has columns. Pretending otherwise means
every downstream consumer needs implicit knowledge that for Excel-sourced
paragraphs the formatting fields are meaningless and `text` is secretly
a Q&A pair — exactly the format leakage the common model was meant to
prevent. Subclassing is worse than peer: it forces a Liskov-substitutable
relationship between two things that share no structural meaning, only
the word "row of content". A second model is honest about what it is,
and the chunker is going to dispatch on source anyway because the
chunking strategy for Q&A rows (one-per-row, optionally split) is
fundamentally different from the chunking strategy for prose (paragraph-
aware with overlap).

**What it teaches.** A shared intermediate representation is the right
abstraction *only when the formats it covers share the same structural
vocabulary*. The moment a new format has a disjoint vocabulary (rows
and columns vs. paragraphs and styles), a second model is cleaner than
contorting the first. The "one common model" principle is a means, not
an end — the end is preventing format-specific knowledge from leaking
downstream, and a second model achieves that better than a forced
abstraction.

## 2. Discover schema, don't assume it — `profile_excel.py`

**Context.** Three RFI Excel files were dropped into `data/`. Quick
inspection shows their schemas have effectively nothing in common:
column letters differ, headers differ, sheet structures differ, and at
least one file isn't even a tabular Q&A at all (it's a form with
labels like "Company:", "Completed By", "Name:"). Hard-coding "the
question is in column B" is how this pipeline breaks the first time a
new client format arrives.

**Options considered.**

- *Hard-coded column mapping per file.* Three constants for three files;
  add a fourth when a fourth file arrives. Fast to write, brittle to
  every new format, mistakes are silent (the wrong column gets ingested
  and answers retrieve attributed to the wrong text).
- *Single canonical template the team enforces on every client.* Real
  in some industries; not realistic here — clients set the RFI format
  and the solutions team has to live with what arrives.
- *Discover the schema at profile time, persist the discovered mapping
  to a config file per source file.* Costs an upfront profile run per
  file; pays off the first time a column moves or a new format arrives.

**Choice and reason.** Discover and persist. The same pattern as
fingerprint-style document profilers — the rules of the format are
inferred from the data, not from the consumer's expectations. Persisting
the discovered mapping to a per-file config keeps the inference cost
one-time and decouples the runtime path (ingest, query) from the
discovery path (profile).

**What it teaches.** When the input shape is controlled by someone
else — clients, partners, upstream systems — assumption is a bug
waiting to happen. The right primitive is *discover + persist + verify*,
not *encode the expected shape in the code*. The cost of discovery is
the price you pay for not breaking every time the shape shifts.

---

## 3. Three layers (heuristic → LLM → human), not one — `profile_excel.py`

**Context.** Discovery still has to produce a *correct* mapping. The
naive options are (a) write rules ourselves, (b) ask an LLM to figure
it out, or (c) make a human do it manually. None of these alone is
robust: rules are brittle on weird files, LLMs occasionally violate
explicit schema constraints, and humans miss subtle bugs in tables of
dense data.

**Options considered.**

- *Rules only.* A hand-tuned classifier on word count, % non-empty,
  cardinality, question-mark rate. Deterministic, debuggable. Fragile
  on files where headers are missing, ambiguous, or in another
  language. No client/date inference.
- *LLM only.* Send the column profile to a model and trust its output.
  Robust to phrasing ("Q." vs "Question" vs "Item"), can infer
  client/date from filename, but **non-deterministic** — running the
  same prompt twice may produce different mappings, and the model
  sometimes invents role names not in the allowed set or assigns the
  same role twice.
- *Human only.* Open every file in Excel, type the mapping by hand.
  No automation cost. Doesn't scale and has no audit trail.

**Choice and reason.** Stack all three, in this order:

1. **Heuristic** computes a starting role guess and the reasoning that
   produced it. Visible to both the LLM and the human.
2. **LLM** sees the column profile *and* the heuristic guess, and
   produces a structured mapping plus a free-text reasoning string.
3. **Validator** runs between LLM and human: exactly one `question`,
   exactly one `answer`, zero-or-one `context`, all roles in the
   allowed set, all sheet columns mapped. Rejects on any violation
   BEFORE the human sees the proposal. (A tired human eyeballing a
   table can miss "this column is both `question` and `answer`".)
4. **Human** approves the validated proposal or refuses. Default-no on
   ambiguous input — the gate is load-bearing, not ceremonial.

Each layer catches a different failure mode. Heuristic catches the
deterministic, easy cases cheaply. LLM catches the phrasing-variation
cases the heuristic can't. Validator catches LLM constraint violations.
Human catches semantic mistakes a machine can't see.

**Empirical confirmation from first run.** Running the profiler on
`Utiq_Publicis RFI.xlsx` (a form-style file, not a clean Q&A table),
the LLM produced a proposal with zero `question` columns — there
genuinely is no question column in that file. The validator rejected
the proposal cleanly and refused to write a config. That is the
validator earning its keep on its first real input. Running on
`Utiq response to The Guardian OpusVerify RFI (1).xlsx` (a clean Q&A
table), all four layers agreed and produced a sensible mapping.

**What it teaches.** When no single layer is robust enough on its own,
stack layers with *different failure modes*. The trick is that the
layers must catch different bugs — stacking three layers that all fail
on the same input is no better than one layer. Heuristic, LLM, and
human fail on disjoint cases, which is what makes the stack robust.

---

## 4. Validate BEFORE showing the proposal to the human — `profile_excel.py`

**Context.** Decision 2 in the spec calls out a specific failure mode:
the LLM occasionally violates explicit schema constraints despite
clear instructions. Where does the validation gate go?

**Options considered.**

- *After the human approves.* Validate at the moment of writing the
  config. Simple. Means the human has already said "yes" to a broken
  mapping, then we throw an error. Confusing UX; the human now has to
  judge whether their approval was wrong or the validator was wrong.
- *Before the human sees the proposal.* Validate the LLM output, and
  only render the proposal to the human if it passes. Failures
  surface as a clear "the LLM produced an invalid mapping — re-run"
  message, not as a post-approval crash.

**Choice and reason.** Before. The principle: the human approval gate
should only ever evaluate proposals that are *mechanically sound*.
A broken mapping is not a judgement call — it's a bug. Show only
candidates that pass mechanical checks; let the human focus their
attention on the semantic correctness no machine can verify.

**What it teaches.** Approval gates should not be asked to do work
machines can do. If a check is deterministic — "exactly one question
column", "all roles in this set" — run it before the human sees the
output. Reserve human attention for the judgements only humans can
make. Conflating the two erodes trust in the approval gate: a human
who has rejected a "valid"-looking proposal once because a machine
check fired post-approval will start rubber-stamping the next one.

## 5. Real-data discoveries: sheet auto-pick and header auto-detect — `profile_excel.py`

**Context.** v1 of the profiler shipped with two assumptions about
RFI Excel files: (a) the right sheet is the one with the most rows,
and (b) the header is row 1. Running v1 against the four real files
in `data/` produced three distinct file shapes and revealed both
assumptions were wrong.

**What we found.**

- *Guardian OpusVerify* — clean 3-column Q&A with an empty
  `Sheet1`-style row 1 and the actual header in row 2 (`Question | Answer`).
  v1 worked on this file *by accident* — its row 1 was empty so row 1
  "as header" turned out fine.
- *INTERNAL Reach DPIA* — clean Q&A with a labelled header in row 1.
  v1 worked.
- *Utiq_Publicis RFI* — form-style. Rows 1..11 are a metadata block
  ("Company:", "Completed By:", "Name:"). The actual Q&A starts at
  row 13. v1 profiled the form block as if it were the data,
  produced no `question` column, and the validator correctly refused
  to write a config.
- *Utiq_Publicis_2023 Futureproof* — six sheets. The largest by row
  count was an operational plan ("EMEA Cookieless Co-op Program",
  595 rows), not the RFI. The actual RFI sheet ("2023 Future Proof
  Questionnaire") was second-largest. v1 picked the wrong sheet AND
  the picked sheet had a row 1..5 preamble — two bugs at once.

**Two improvements landed.**

1. *Sheet selection by question-mark density.* Walk every sheet,
   count cells whose stripped text ends with `?` in the first 200
   rows. Pick the sheet with the most. Fall back to row-count as a
   tiebreak. Rationale: the *only* reliable signal that a sheet
   contains RFI questions is that it contains questions. Counting
   `?`-ending cells is the cheapest proxy for "this is a Q&A sheet",
   and on the Futureproof file it picked correctly with 73 `?`-cells
   vs 0 on the operational plan.

2. *Two-pass header-row auto-detect.* Pass 1 walks rows 1..50 looking
   for an exact-match header label (`question`, `answer`, `response`,
   etc., case-insensitive, single-letter aliases excluded to avoid
   noise). Pass 2 only runs if pass 1 finds nothing: walk rows for
   the first cell ending `?` with 5+ words, use the row above as the
   header. Default to row 1 if neither pass fires.

   The order matters. Pass 2 alone (which is what I built first)
   regressed the Guardian file: that file has some questions ending
   `?` and some phrased as directives ("Describe your approach to..."),
   and pass 2 would skip the directive rows because they didn't fire
   the `?`-signal. Pass 1's label-match is *lossless* — it locks onto
   the labelled row deterministically and the data window stays
   intact. Pass 2 is the fallback for files (like Utiq_Publicis RFI)
   where no label exists.

3. *Escape-hatch CLI flags.* `--sheet NAME` and `--header-row N`
   override auto-detect when it picks wrong on a file we haven't
   seen. The auto-detect handles the four files at hand; the flags
   handle whatever file 5 turns out to be.

**Empirical confirmation after improvements.** All four files now
pass validation end-to-end. The validator's exactly-one-`question`/
exactly-one-`answer` check stayed silent on every file (no false
rejections). The auto-detect reasons are visible in the profiler's
output (`Sheet selected (auto): X [reason]`, `Header row (auto): N
[reason]`) so a human reading the trace can verify what fired.

**What it teaches.** Specs and assumptions are *hypotheses*, not
ground truth. They look right until real data arrives. The v1
profiler was internally consistent and looked sensible on the spec's
example shape — running it on four real files exposed both the
sheet-selection bug and the header-row bug within twenty minutes.
The lesson: ship the simplest version that compiles, then *run it on
the real data immediately*. Don't over-design v1; design v2 from
v1's empirical failures. The work between v1 and v2 was small (~80
lines) but it was the right work, because real data drove it.

A related lesson: when a heuristic has multiple signals, ordering
matters. Label-match before `?`-density is the right order because
label-match is lossless and `?`-density is lossy. Picking the
right ordering is the difference between a robust heuristic and one
that regresses on a previously-working file.

## 6. Excel loader: persist discovery output, detect section markers — `loaders/excel_loader.py`

**Context.** Step 2's profiler discovers per-file structure (sheet,
header row, column-to-role mapping, client/date). Step 3's loader
materialises that mapping into `Row` dataclasses for the chunker.
The first question is what the loader knows vs. what it asks the
profiler to have decided ahead of time.

**Persist discovery output, do not re-discover.** The config schema
gained a `header_row` field at the same time the loader was written.
The reason: header_row is now load-bearing — form-style RFIs have
header_row > 1, and a loader that defaults to row 1 silently mis-reads
those files (header cells loaded as Q&A, preamble loaded as data).
Persisting header_row in the config means the loader doesn't re-run
the profiler's auto-detect heuristic — discovery happens once at
profile time, the human approves it, and the loader treats the
config as ground truth. The loader's `_validate_config` raises a
clear error if it finds an old config without header_row, telling
the user to re-profile.

This is the same pattern as the rest of the pipeline: discovery is
expensive and human-validated; runtime is deterministic and config-
driven. Loaders should never re-do work the profiler already did.

**Section markers: detected, not enforced.** Running the loader on
the four real files surfaced a pattern the profiler doesn't capture:
"section divider" rows — short labels like `Audiences/Targeting`,
`Campaign Management`, `Direct consent` sitting in the question
column above the Q&A rows for that section, with the answer cell
empty. Loading these as Q&A pairs would corrupt the corpus: a chunk
whose text is just "Audiences/Targeting" has no semantic value and
pollutes retrieval against any query.

Diagnostic across the four real files (before section detection):

| File | rows | short Qs (≤4 words, no '?') |
|---|---|---|
| Utiq_Publicis RFI | 28 | 10 |
| Utiq_Publicis_2023 Futureproof | 169 | 46 |
| INTERNAL Reach DPIA | 81 | 0 |
| Guardian OpusVerify | 50 | 0 |

The detector in `_is_section_marker` is intentionally conservative:
question is 1–4 words AND not ending in `?` AND answer/context/all
configured metadata cells are fully empty. Any signal of "this row
carries content" (an answer, a category, anything) defeats the
section-marker classification — the row falls through and gets
loaded normally. Conservative because the cost of a false-negative
(load a section marker as Q&A) is one polluted chunk; the cost of a
false-positive (drop a real short Q&A pair) is silently losing
training data. Picking the asymmetry that minimises the silent loss
is the right call.

When a section marker is detected, the value is captured into
`current_section` state and propagated into `metadata['section']`
of every subsequent loaded row until the next marker. If the file
has an explicit `section` column (file 4 / Guardian — the LLM
identified Column C as `section` in Phase 2 of profiling), that
column's value lands in metadata first and the inferred section
defers to it. Explicit data always wins.

After section detection, the diagnostic re-ran:

| File | rows (after) | sections discovered |
|---|---|---|
| Utiq_Publicis RFI | 22 (was 28) | 6 |
| Utiq_Publicis_2023 Futureproof | 140 (was 169) | 10 |
| INTERNAL Reach DPIA | 81 (unchanged — no markers) | 0 |
| Guardian OpusVerify | 50 (unchanged) | 14 (from column) |

35 polluting section-marker rows stripped across the corpus; every
loaded row now carries a section attribute where one was inferable.

**What it teaches.** Two related lessons. First: when discovery
produces structured output that downstream stages depend on, persist
that output rather than re-running the discovery. Re-discovery
couples downstream behaviour to the discovery heuristic; persistence
decouples them. Second: when a data-quality pattern appears in real
data that the spec didn't anticipate, the *conservative* response is
right. Section markers are real, they corrupt retrieval, and they
must be handled — but the detector should be biased toward
false-negatives (load too many) rather than false-positives (drop
real data), because silent data loss is the harder failure to
diagnose. Stupid heuristics that you can explain are better than
clever heuristics you can't audit.

## 7. Loader skip rule tightened from "both empty" to "question empty" — `loaders/excel_loader.py`

**Context.** The v1 loader skipped a row only when both the question
and the answer cells were empty. The reasoning at the time was that
an "asked but unanswered" row (question present, answer blank) was
useful to keep, and the symmetric case (answer present, question
blank) was rare. That symmetry held only until the loader was run on
the four real RFI files. File 3 (`INTERNAL - Reach Customer facing
DPIA questions.xlsx`) produced 14 rows in the form Q='', A='no'
because the Phase 2 LLM mapped column A (the "Gateway Questions"
column with the actual question text) as `context` and column B as
`question`. The "real" question lived in `context`; the `question`
cell was blank.

**Options considered.**

- *Keep the BOTH-empty rule, hand-fix file 3's config.* The cleanest
  surgical option, but requires per-file manual editing every time a
  file like this shows up. The spec is meant to handle "files we
  haven't seen yet" gracefully — one-off hand-editing is fine for a
  fixed corpus but doesn't generalise to new files.
- *Tighten to "question empty -> skip".* Treats a row without a
  question as not a Q&A row, regardless of what the answer cell
  holds. Drops file 3's 14 mis-mapped rows automatically and would
  catch any future file with the same shape without human
  intervention.

**Choice and reason.** Tighten the rule. A row without a question
cannot be retrieved by question similarity — that is the core
retrieval pattern. Keeping such rows in the corpus costs storage and
pollutes BM25 / reranker results with chunks that have no question
text to match against. The benefit (catching an LLM-mis-mapped row
that happened to put real content in the answer cell) is a benefit
to that specific row only — the row is still mis-attributed (its
"answer" is "no" to a question we discarded as "context"). Better
to drop it cleanly than to keep a wrong-shape pair.

The "asked but unanswered" case (question present, answer blank) is
preserved by the new rule — it has Q, so it loads. File 1 keeps its
1 such row, file 2 keeps its 10, file 3 keeps its 3.

**Impact on corpus size.**

| File | Before (v1) | After (this change) |
|---|---|---|
| Utiq_Publicis RFI | 22 | 22 (unchanged) |
| Utiq_Publicis_2023 Futureproof | 140 | 140 (unchanged) |
| INTERNAL Reach DPIA | 81 | 67 (14 mis-mapped rows dropped) |
| Guardian OpusVerify | 50 | 50 (unchanged) |
| **Total** | **293** | **279** |

**What it teaches.** When a heuristic is symmetric and one direction
turns out to be a footgun in real data, the right move is to break
the symmetry, not to add complexity. The original BOTH-empty rule
treated "Q without A" and "A without Q" as the same shape. They are
not: a corpus that retrieves by question similarity needs a question
on every row, but tolerates a missing answer. The asymmetry of the
data demands the asymmetry of the rule. Designing the v1 rule
symmetrically was wrong; the fix is one line of code.

A meta-lesson: the cost of running v1 on real data and observing the
output is what makes the v2 rule obvious. v1's reasoning ("BOTH
empty is the cleanest definition") was internally consistent and
defensible in the abstract. Real data won the argument.

## 8. Chunk reviewer is read-only and shares its builders with ingest — `review_rfi_chunks.py`

**Context.** Spec Step 4 inserts a human approval gate between
"discovery + loading" and "embedding + ChromaDB". The Step-3 loader
produces 279 `Row` objects across four files; Step 5 will embed
them and write to ChromaDB; in between, the reviewer prints the
chunks that *would* be embedded and asks the human if they look
right. The interesting design question is what the reviewer should
*do* relative to what it shows.

**Options considered.**

- *Reviewer builds its own preview format, ingest uses a different
  one.* Two code paths for chunk construction. Easiest to write but
  the worst design — the reviewer's preview can drift from the
  ingester's actual output, and the human approves a chunk shape
  that isn't quite what gets embedded.
- *Reviewer calls a chunk-builder library, ingest calls the same
  library.* One source of truth. Picked.
- *Reviewer triggers ingest on 'yes'.* Tighter coupling, fewer
  steps for the human, but loses the property "rerun the reviewer
  without consuming embedding API calls". Rejected.

**Choice and reason.** `review_rfi_chunks.py` defines
`build_combined_chunks(rows) -> list[dict]` and
`build_separated_chunks(rows) -> list[dict]`, prints their output
verbatim, and stops. Step 5's `ingest_rfi.py` will import and call
the same two functions to construct the chunks it sends to
`mistral-embed`. The chunk shape the human sees IS the chunk shape
that lands in ChromaDB; there is no second representation.

The reviewer does no DB call, no Mistral call, no file write. It
exists so the human can iterate (re-profile a file, re-edit a
config, re-load, re-review) without burning embedding API calls or
polluting the vector store.

**On using dicts rather than a `Chunk` dataclass.** ChromaDB's
`collection.add(documents=..., metadatas=...)` API accepts parallel
lists of strings and dicts. A `Chunk(text, metadata)` dataclass
would add a layer with no behaviour beyond what the dict already
expresses. The dict matches the API exactly; the builders return
exactly what the embedder + DB layer want. The `Row` dataclass
remains the typed representation upstream of chunking — that one
earns its dataclass-ness because it's the format-agnostic
intermediate model that the chunker dispatches on.

**On empty-answer chunks under Strategy B.** The loader (per
entry 7) keeps rows where the question is present but the answer
is blank — these are "asked but unanswered" rows, 14 of them
across the corpus. Strategy A bundles them into a single chunk
where the question text dominates the word count. Strategy B emits
them as two chunks: a question chunk (non-empty) and an answer
chunk (empty string). The reviewer surfaces this with
`min_words: 0` on Strategy B's aggregate stats. The decision of
what to do about empty answer chunks belongs to Step 5 (ingest) —
either skip the empty answer chunk before embedding, or send it
and accept whatever `mistral-embed` does with empty input. The
reviewer's job is to make the situation visible, not to silently
prune.

**What it teaches.** When two stages of a pipeline both need to
construct the same object, factor the construction into a function
both call. The temptation under time pressure is to copy-paste,
which compiles fine and works fine until the constructions drift
out of sync — at which point the bug is silent and hard to find.
A shared builder is also a place to put the structural decisions
(prefix wording, context handling, metadata fields) so they live
in one location and the human approval gate is asking about *the*
chunk shape, not *a* chunk shape.

## 9. Ingestion: four collections, per-file checkpointing, empty-text filter — `ingest_rfi.py`

**Context.** Step 5 is the first stage of the pipeline that calls
the embedding API and writes to ChromaDB. Three design choices
shaped the script.

**Four collections, one per (strategy × distance metric).** The
experiment matrix in the spec requires comparing cosine vs L2
under both Strategy A (combined) and Strategy B (separated). The
ChromaDB constraint that bit early: a collection's distance metric
is set at creation time and is *immutable* — you cannot switch
metric at query time. So the four collections are built up front,
each with `metadata={"hnsw:space": "cosine" | "l2"}`. Switching at
query time would have been a cleaner API but isn't on offer; given
that, four collections is the right materialisation of the
experiment matrix. Storage cost is negligible — 1024-dim vectors
× ~1,650 chunks is rounding error.

**Per-file checkpointing, not per-batch and not per-collection.**
The unit of resumable work is one (collection, source_file) pair,
recorded in `outputs/.ingest_checkpoint.json` after each pair
completes. Trade-offs considered:

- Per-batch checkpoint: more granular but extremely chatty (16-chunk
  batches × 4 collections × 4 files = ~140 writes per full run);
  recovery is more complex (which batch of which file is "next").
- Per-collection checkpoint: simpler but loses up to a whole
  collection's worth of work to a transient failure mid-collection.
  The empirical run hit four 429 rate-limits during the Strategy B
  L2 collection; per-collection checkpoint would have lost all
  progress for that collection on the worst-case failure.
- Per-file checkpoint: matches the natural failure recovery
  granularity. Lose at most one file's embedding work to a
  catastrophic failure; resume picks up at the next file. Picked.

The choice is informed by `call_with_retry`'s behaviour: the helper
absorbs transient 429s and 5xxs invisibly. The empirical evidence
from this run shows it working: four 429s caught and retried during
ingest, zero batches lost, zero manual intervention. The
checkpoint only kicks in when retries are *exhausted* (sustained
outage) — which is exactly the regime where you want resumable
work.

**Empty-text chunks dropped before embedding.** Following the
loader's decision (entry 7) to keep "asked but unanswered" rows
in `list[Row]`, Strategy B emits an empty-string answer chunk for
each such row. Sending an empty string to `mistral-embed` either
errors or returns a zero vector — either way the chunk pollutes
retrieval. The ingester filters chunks where `text.strip() == ""`
before the embed call; the paired question chunk is still embedded
normally. Across the four real files, 14 answer chunks were
filtered (3 + 10 + 1 + 0 by file). The Strategy B collections
landed at 544 chunks each (279 + 279 − 14) rather than the naive
558.

**Empirical results.** Full ingestion succeeded end-to-end on the
real corpus:

| Collection | Chunks | Source |
|---|---|---|
| rfi_combined_cosine | 279 | 1 chunk per row |
| rfi_combined_l2 | 279 | 1 chunk per row |
| rfi_separated_cosine | 544 | 2 per row, − 14 empty answers |
| rfi_separated_l2 | 544 | 2 per row, − 14 empty answers |

A spot-check semantic query — *"What is your approach to GDPR
compliance?"* — against `rfi_combined_cosine` returned the three
most-on-topic rows in the corpus (all from the Publicis Futureproof
file, all about privacy regulation compliance), with cosine
distances in a tight 0.21–0.24 band. End-to-end retrieval works.

**Other small calls.**

- *Metadata sanitisation.* ChromaDB doesn't accept None or empty
  strings in metadata in some versions. The ingester strips them
  before add. The semantic loss ("this row has no date inferred")
  is preserved by the absence of the key rather than a sentinel
  value; filtered retrieval still works for rows that do have
  dates.

- *Stable IDs.* `<pair_id>` for combined chunks, `<pair_id>__role`
  for separated. pair_id is globally unique (slug includes the
  filename), so identifiers are stable across re-ingests. The
  checkpoint prevents accidental duplicate-id errors by skipping
  already-ingested (collection, source_file) pairs.

**What it teaches.** When a step has API cost and DB-write side
effects, design for resumability *before* you find out the hard way
that you need it. The cost of writing per-file checkpoint logic
upfront is ~30 lines of code; the cost of *not* having it the first
time the embedding service goes intermittent is "re-embed
everything you already did". The same pattern of "save the
expensive thing as soon as you have it" applies to web scrapes,
batch jobs, training runs — anywhere a partial result is more
valuable than nothing.

A related lesson: `call_with_retry` and per-file checkpointing are
layered defences against different failure modes. Retry handles
transient blips (429, 5xx, network) at the API-call level.
Checkpointing handles sustained outage and process-level crashes
at the work-unit level. Together they make the ingester robust to
both. Picking just one of the two leaves a hole that the other
covers.

## 10. Query: three retrieval modes, three rerankers, role-filtered separated, refusal-guarded generation — `query_rfi.py`

**Context.** Step 6 ties together everything before it: take a
natural-language question, find the most-relevant past Q&A pairs
in ChromaDB, optionally reapply a more precise relevance signal,
and (for Strategy B) fetch the paired answers via the linkage the
chunker established. The spec asks for three retrieval modes
(semantic, BM25, hybrid) and three rerankers (none, LLM,
crossencoder) — the full experiment matrix the eval step will
compare. Several non-obvious design calls landed.

**Retrieval-pool sized for reranking, not for the final answer.**
The CLI exposes `--pool-size` (default 20) and `--top-k` (default 3).
Retrieval returns the pool; the reranker chooses the top-k from
that pool. The pool has to be wide enough that the reranker can
actually improve over retrieval's ranking; if the pool is already
top-3, the reranker just rubber-stamps. 20-pick-3 is the spec's
recommended shape and it matches the empirical behaviour observed
here — the crossencoder routinely promotes a chunk from rank 4-6
into the top-3 because the cross-encoder reads the (query, chunk)
pair carefully where semantic alone was approximating.

**Three retrieval modes, complementary failure modes.**

- *Semantic* embeds the query with `mistral-embed` and asks
  ChromaDB for nearest neighbours. Strong on paraphrases:
  "GDPR compliance" surfaces "privacy legislation adherence" even
  though those phrases share no tokens. Weak on exact terminology
  (an acronym query may rank a thematic match higher than the
  literal-acronym chunk).
- *BM25* scores `tf × idf` on tokenised text. Strong on exact
  terms, acronyms, regulatory refs, product names. Weak on
  semantic paraphrase — "data retention policy" misses a chunk
  about "how long we keep information".
- *Hybrid* via Reciprocal Rank Fusion (k=60) merges both rankings.
  Empirically, on the GDPR test query: semantic ranked Guardian
  row 29 at #4 and BM25 ranked it at #3; the fused score put it
  at #2. Neither signal alone surfaced it that high.

RRF was chosen over learned weight fusion because it requires no
training data, no per-query tuning, and is robust to score-scale
differences (one ranking returns distances 0..1, the other
returns BM25 scores 0..15). Spec Decision 4 calls this out
explicitly.

**Three rerankers, different cost/quality profiles.**

- *none*: zero overhead, just `pool[:top_k]`. The retrieval
  ranking is what the generator sees.
- *crossencoder* (`cross-encoder/ms-marco-MiniLM-L-6-v2`):
  local, ~50 ms per (query, chunk) pair. No API cost per query.
  Brings ~600 MB of torch + transformers as transitive deps —
  the image bloat happens at build time, not query time. Runs
  via a lazy import so queries that don't use it pay no import
  latency.
- *llm* via mistral-small-latest: one extra API call per query
  (~1200 input tokens, small JSON output). Cheap per call but
  not free. Robust to nuance — empirically picked the same
  top-3 as the crossencoder on the GDPR test query, just with
  different scoring semantics.

The crossencoder is the production-realistic default because it
scales linearly with chunk count without per-query API cost.
The LLM reranker is included because it's a good teaching tool
("here is what reranking is doing") before swapping in the
optimised version. Spec Decision 5 makes the same recommendation.

**Role filter is load-bearing for separated collections.**
This one I got wrong on the first attempt. For Strategy B,
question chunks and answer chunks both live in the same
ChromaDB collection (distinguished by `metadata.role`). The
retrieval API returned both — and the crossencoder happily picked
answer chunks because they have more text and so more keyword
overlap with the query. The Q→A linkage step then tried to fetch
the paired answer for what was already an answer chunk and
crashed on `DuplicateIDError`.

The fix is a `where={"role": "question"}` filter passed to
ChromaDB's `query()` and to `collection.get()` for the BM25
corpus. Question-to-question matching is the entire point of
Strategy B — mixing answer chunks into the retrieval pool
defeats it. The filter applies at the DB level (cheap) rather
than as a post-hoc Python pass.

**Q→A linkage by id lookup, not metadata WHERE.** After retrieval,
each top-k question chunk has id `<pair_id>__question`. The
paired answer is `<pair_id>__answer`. A single
`collection.get(ids=[...])` call resolves the linkage —
deterministic, indexed, no metadata scan. ChromaDB's id index
makes this an O(k) operation. Using `where={"pair_id": ...}`
would force a metadata filter scan per chunk, which is fine on
small collections but won't scale.

A defensive corner: some question chunks have no paired answer
because the original Excel row had an empty answer cell and the
Step 5 ingester filtered it out. The fetcher returns a placeholder
text "(answer not found)" rather than raising — the generator
sees the missing answer alongside the present ones and decides
what to do.

**Hallucination guard in the generation prompt.**
The generation prompt ends with: *"If the past Q&A pairs don't
cover the question, reply exactly: 'I cannot find this in our
corpus.'"*. Without this, Mistral confabulates plausible answers
when the corpus is silent on a topic. With it, refusals are
distinguishable from real answers — which is exactly what spec
Decision 6 calls out as the "hallucination refusal vs retrieval
gap" distinction the eval framework needs to measure.

Empirically: a query "What is the airspeed velocity of an
unladen swallow?" against the GDPR-laden corpus correctly
returned *"I cannot find this in our corpus."* even though the
retrieval still returned its best three keyword/embedding matches
(none of which were related to airspeeds or swallows). The guard
fires when context doesn't cover the question, not when retrieval
fails to find anything.

**What it teaches.** Three lessons.

First: when a system has two related notions (question chunks and
answer chunks for the same row), be explicit about which one each
stage operates on. The role filter bug came from a tacit "the
retrieval pool is the right thing to rerank" assumption that was
true for combined collections and false for separated. Make the
implicit explicit.

Second: stacking layers (retrieval → rerank → generate) is only
useful if each layer can improve over the one below. Reranking
the top-3 directly is a no-op; reranking a top-20 pool can
promote rank-4 to rank-1. Pool size and final-k must be chosen
together.

Third: the refusal path is part of the system, not an edge case.
The hallucination guard is one sentence in the prompt; without it,
the system fails silently on out-of-scope questions, which is the
hardest failure mode to detect because the output *looks* like a
real answer. The eval framework's "hallucination refusal rate"
and "retrieval gap rate" metrics exist specifically to measure
this — and require the system to actually refuse when it should.

## 11. Eval framework: separate the two refusal rates, reuse query — `eval_rfi.py`

**Context.** Spec Step 7 is the experiment matrix. It runs the full
36-configuration grid (4 collections × 3 retrieval modes × 3
rerankers) against a 20-question ground-truth set and produces a
comparison table sorted by retrieval quality + LLM-judge scores.
Three decisions were the load-bearing ones.

**Retrieval gap vs hallucination refusal are reported separately.**
Both produce identical-looking refusal output ("I cannot find this
in our corpus."). They mean opposite things:

  - *Hallucination refusal* — system working correctly, the corpus
    has no answer to an out-of-scope question.
  - *Retrieval gap* — system FAILING, the corpus does contain a
    correct answer but the pipeline didn't surface it.

If the eval averaged them into a single "refusal rate", a high
score would be ambiguous: is the system being conservatively
honest, or silently broken? Spec Decision 6 calls this out
explicitly. The eval splits by the `scope` field of each test
question — in-scope refusals contribute to `retrieval_gap_rate`,
out-of-scope refusals contribute to `hallucination_refusal_rate`,
and the two ratios appear as separate columns in the comparison
table.

A high `retrieval_gap_rate` flags a retrieval bug. A LOW
`hallucination_refusal_rate` flags a generator that's confabulating
answers — both are first-class signals.

**Reuse query_rfi.py's functions verbatim.** The eval imports
`retrieve_semantic`, `retrieve_bm25`, `retrieve_hybrid`,
`rerank_none`, `rerank_crossencoder`, `rerank_llm`,
`fetch_paired_answers`, and `generate_answer` from query_rfi. The
eval IS the query system on a benchmark. If the query module's
behaviour changes (e.g. the role filter for separated collections
that was added during query_rfi testing), the eval automatically
sees that change. No second implementation can drift out of sync
with the first. This is the same shared-builder pattern that the
chunk reviewer applied to chunk construction (entry 8), applied
here to the retrieval + generation path.

**The LLM judge is skipped on refusals.** A refusal text would
trivially score faithfulness=5 ("faithful to the empty context")
but relevance=1 ("doesn't answer the question"). Including
refusals in the judge's averages would muddy both metrics and
give a misleading picture of when the answer-generation step is
producing useful content. The eval reports judge scores over the
*in-scope, non-refused* subset only; the refusal counts are
reported as their own rates. The judge runs on the subset of
questions where there's actually an answer to score.

**Checkpoint granularity = one full configuration.** Each
configuration is one (collection, retrieval, rerank) triple
running all 20 questions — about 30-60 API calls of work. The
checkpoint saves after every configuration completes. Finer
granularity (per-question) would bloat the checkpoint and
complicate aggregate computation; coarser granularity
(per-collection or per-strategy) would risk losing 9+
configurations to a single failure. One config is the natural
unit of resumable work, the same way one file was the natural
unit for ingest (entry 9).

**The dataset is not committed to git.** `outputs/eval_dataset.json`
contains 17 in-scope questions whose `expected_pair_id` fields
include the slugified filenames of the source RFIs — which
contain client names ("Utiq_Publicis", "The Guardian", "Reach").
Same privacy boundary that keeps `config_rfi_*.json` gitignored.
The dataset is reproducible: the script that produces it
(currently a manual draft) can be re-run; a new owner of the
repo authors their own dataset against their own corpus.

**What it teaches.** When two metrics produce identical *output*
but mean opposite things, the eval is the only place to keep
them distinct — code doesn't care which kind of refusal it just
emitted, but the human looking at the comparison table does. The
asymmetry of "in-scope vs out-of-scope expectations" has to be
encoded in the test data itself (scope tags), not derived from
behaviour. A naive eval would collapse both into a single
"refusal rate" and the comparison table would be telling you
nothing actionable.

The reuse pattern earns its keep here for the second time. The
chunk reviewer (entry 8) imports the chunk builders from
itself. The eval (this entry) imports retrieval + generation
from query_rfi. Both cases: *the script doing the human-facing
work and the script measuring quality run identical code*. There
is no "production path vs measurement path" drift to debug.

## 12. Hand-verification with a domain expert — what the formal eval can't measure

**Context.** Between writing the eval framework and running the full
36-configuration matrix, three real RFI questions arrived from a
domain expert (CPO). The questions covered privacy/regulatory
compliance, security measures, and a yes/no capability question —
the same shapes the eval dataset paraphrases. They were a chance to
stress-test the production-realistic configuration
(`rfi_separated_cosine` + hybrid + crossencoder + top-k=3) against
hand-picked queries before any automated scoring.

**What hand-verification surfaced that automated metrics wouldn't.**

- *The provenance trace is the headline output, not the answer.*
  The expert's positive feedback was specifically about seeing
  *which* past Q&A pairs contributed to each answer, with the
  crossencoder scores attached. That's not a metric the eval
  framework measures — Recall@3 records "was the right chunk in
  top-3" as a binary, but it doesn't capture "was the user shown
  *why* the system picked these chunks." The verbose-provenance
  default in `query_rfi.py` is a usability decision the eval
  cannot validate. (Saved as feedback memory; future RAG outputs
  in this codebase will default to verbose-provenance.)

- *The score gap between top-1 and top-2 is a confidence signal
  to a human.* On the three CPO questions, top-1 had crossencoder
  scores 4x–9x higher than top-2 — visibly "the system is
  confident" rather than "the system is guessing between two
  candidates". Recall@3 / MRR aggregate this signal away
  completely (a top-1 score of 9 and a top-1 score of 0.1 both
  count as recall=1 if the right pair_id is at rank 1). For a
  human reading retrievals, the gap is real signal: it says "if
  this answer is wrong, the system would have known".

- *Near-exact wording matches reveal the corpus's shape, not
  just the system's quality.* For two of the three CPO questions
  the corpus contained a past question with essentially
  identical wording. The retrieval got those right at rank 1
  with high scores — but that says as much about the test
  question being well-trodden ground as about the system. The
  harder eval signal is questions where no near-exact past
  question exists, which is what the eval dataset's
  paraphrasing is meant to provide. Hand-verification flags
  which test queries are too easy.

- *Generated-answer quality has dimensions the LLM judge
  doesn't capture.* The judge scores faithfulness, relevance,
  completeness. A domain expert reading the answer also judges
  things like phrasing-fitness-for-a-real-RFI-response
  (corporate vs casual tone, hedging vs declarative voice,
  attribution to specific documents like "DPIA Overview" vs
  generic gestures). Those qualities are part of "is this
  actually shippable as an answer", and only a human reader
  catches them.

**What it teaches.**

Two things. First, automated eval and hand-verification measure
different qualities, and a healthy build uses both. Automated
eval scales — it can grind through 36 configurations × 20
questions and rank them quantitatively. Hand-verification
doesn't scale, but it catches usability and tone problems
automated metrics are blind to. Skipping either in favour of
the other is a category error.

Second, the score gap between top-1 and top-2 (and more broadly
the *distribution* of scores in the retrieval pool) carries
calibration information that scalar aggregates discard. A
production system that surfaces only top-k chunks without scores
hides this from the user. The pattern emerging here — "show
retrievals with scores BEFORE the answer" — is doing real work
beyond just making the system feel transparent. It's letting the
reader calibrate their trust in each answer on the fly.

## 13. Eval results: production recommendation + the surprises real data delivered

**Context.** The full 36-configuration matrix completed: 4 collections
× 3 retrieval modes × 3 rerankers × 20 questions (17 in-scope, 3
out-of-scope). The data lives in
`outputs/rfi_validation/eval_results.json` and
`outputs/rfi_validation/comparison.md`. This entry distils the
headline findings and lands a production recommendation per the
spec's Definition of Done.

**Headline: every config got hallucination refusal exactly right.**
HallucRefusal = 1.000 in all 36 configurations. All three
out-of-scope questions ("airspeed velocity of an unladen swallow",
"how to cook risotto", "FIFA World Cup 2022 winner") were refused
with "I cannot find this in our corpus." across every (collection,
retrieval, rerank) combination. The hallucination guard in the
generation prompt is doing its job — the system never fabricates.
This is the single most important calibration property of the
system, and the eval confirms it.

**LLM-judge over-scores. Real signal lives in retrieval-gap and
completeness.** Faithfulness = 5.00 and Relevance = 5.00 across
all 36 configurations. Either the answers are uniformly perfect
(unlikely) or `mistral-small` as a judge is too generous on those
axes (the more probable explanation). Completeness shows variation
(4.33 to 4.86), and retrieval-gap shows real variation (0.176 to
0.471). Those two are the actionable comparison metrics. The flat
faithfulness/relevance scores are themselves a finding — a
production deployment that relies on LLM-as-judge for ongoing
quality monitoring needs a tougher rubric, or a different judging
model, or paired-comparison rather than absolute scoring.

**Findings by axis (means across the configurations sharing that
axis value):**

*Retrieval mode:*
- semantic: R@3=0.990, MRR=0.940, RetrGap=0.275  ← best
- hybrid:   R@3=0.971, MRR=0.944, RetrGap=0.294
- bm25:     R@3=0.951, MRR=0.922, RetrGap=0.343  ← worst

Counter to the spec's intuition, hybrid does NOT beat semantic on
this corpus. Likely reasons: the corpus is small (280–540 chunks
per collection), the test questions are close paraphrases of
corpus questions so semantic similarity is already strong, and
BM25 occasionally promotes high-token-overlap chunks that aren't
topically relevant. RRF's contribution is small when semantic
alone is near-saturated; on a larger or more terminology-heavy
corpus the hybrid advantage would likely reappear.

*Rerank mode:*
- crossencoder: R@3=0.990, MRR=0.966, RetrGap=0.328
- none:         R@3=0.971, MRR=0.912, RetrGap=0.324
- llm:          R@3=0.951, MRR=0.927, RetrGap=0.260  ← best on gap, completeness 4.67

Crossencoder wins on retrieval precision (right chunk at rank 1)
but LLM rerank produces the lowest refusal rate AND highest
completeness (4.67 vs 4.57 for crossencoder). The split is
real: crossencoder rates "which chunk is most semantically
relevant to the query"; the LLM rerank rates "which chunks together
will help generate an answerable response", which is a slightly
different criterion. The LLM rerank pays one extra API call per
query for that quality lift.

*Chunking strategy:*
- separated: R@3=0.980, MRR=0.955
- combined:  R@3=0.961, MRR=0.915

Separated wins, marginally. The spec's intuition that
question-to-question matching is more precise than Q-and-A-blended
matching is borne out, but the margin is 2%/4% — not the
landslide the spec implied. With a stronger reranker the gap
narrows further. Combined survives as a real alternative if
implementation simplicity matters.

*Distance metric:*
- cosine: R@3=0.967, MRR=0.931
- l2:     R@3=0.974, MRR=0.938

L2 has a small but consistent edge. Both metrics are L2-normalised
(mistral-embed produces unit-norm vectors), so L2 distance and
cosine distance are mathematically related — the differences
empirically observed are within measurement noise on a 17-question
in-scope set.

**Top 5 configurations by lowest retrieval-gap (among those with
Recall@3 = 1.0):**

| Rank | Collection | Retrieval | Rerank | RetrGap | Compl |
|---|---|---|---|---|---|
| 1 | rfi_combined_l2 | semantic | llm | 0.176 | 4.86 |
| 2 | rfi_separated_l2 | hybrid | none | 0.176 | 4.57 |
| 3 | rfi_combined_cosine | semantic | none | 0.235 | 4.46 |
| 4 | rfi_combined_l2 | semantic | crossencoder | 0.235 | 4.46 |
| 5 | rfi_separated_cosine | semantic | crossencoder | 0.235 | 4.62 |

**Production recommendation.**

Two candidates depending on how much budget the production system
has per query:

*If per-query latency and API cost matter (default):*
> **`rfi_separated_cosine` + `semantic` + `crossencoder` + top-k=3**
>
> Recall@3 = 1.000, MRR = 0.971, RetrGap = 0.235, HallucRefusal =
> 1.000, Completeness = 4.62, Faith/Rel = 5.00.
>
> Crossencoder runs locally (one-time image bloat, no per-query API
> cost). Semantic retrieval is the single strongest signal on this
> corpus. Separated strategy gives the best retrieval metrics and
> supports the cleanest Q→A linkage. Cosine is the conventional
> embedding similarity metric (L2 marginally better but cosine
> matches typical ChromaDB / embedding-API defaults — less
> surprising for future maintainers).

*If completeness and refusal rate matter more than per-query cost:*
> **`rfi_combined_l2` + `semantic` + `llm` rerank + top-k=3**
>
> Recall@3 = 1.000, MRR = 0.931, RetrGap = 0.176 (lowest), Compl =
> 4.86 (highest).
>
> Pays one extra `mistral-small` call per query for the relevance
> rerank, in exchange for substantially fewer "I cannot find this"
> refusals on in-scope questions and higher completeness scores.
> Worth it if a polished response is the product, not the
> retrieval list.

**What it teaches.**

Three lessons.

First, spec intuitions are hypotheses, and the eval is what
adjudicates them. The spec leaned toward hybrid retrieval +
separated + cosine + crossencoder as the default. The data says
semantic beats hybrid on this corpus, separated wins by a
narrower margin than expected, L2 marginally beats cosine, and
LLM rerank actually edges out crossencoder on the metrics that
matter most. Without the eval, the production system would
plausibly have shipped a sub-optimal config. The eval is doing
real work, not just confirming priors.

Second, LLM-as-judge has a calibration problem worth designing
for. Faithfulness = 5.00 and Relevance = 5.00 across every
configuration is not credible. A judge prompt that allows
gradations only at the top (5/5 on a 1-5 scale) is one that
can't discriminate good from very good. For ongoing production
monitoring, options include: (a) anchor the judge with explicit
counter-examples ("a 5 means X; a 3 means Y; a 1 means Z"), (b)
use paired-comparison ("which of these two answers is better?")
where the judge picks one rather than scoring both at the
ceiling, or (c) trust retrieval_gap and completeness as the
distinguishing metrics and accept that f/r are
operating-correctly checks rather than ranking signals.

Third, "Recall@3 = 1.0 doesn't mean the system answered." Many
configurations hit perfect Recall@3 but still refused on 18-47% of
in-scope questions. The refusals come from a mix of (a) corpus
gaps where the retrieved chunk's answer cell is empty (e.g. the
Reach DPIA file has 3 "asked but unanswered" rows), (b) cases
where the retrieved chunk is the right *topic* but the
generation prompt judges the context insufficient to answer
confidently. This is the price of a strong hallucination guard
— the system errs toward refusal. The retrieval_gap_rate is the
right metric to monitor that trade-off; it has to be reported
separately from hallucination_refusal_rate for either signal to
be actionable.

**Open questions worth investigating later.**

- *Which specific in-scope questions get refused?* The
  retrieval_gap floor (0.176 = 3 of 17 questions) probably tracks
  the empty-answer rows in the corpus. Verifying this would
  confirm the gap is a corpus property, not a system bug.
- *Does the hybrid advantage emerge at scale?* This corpus is
  ~280-540 chunks per collection. With 5,000+ chunks and more
  acronym-heavy queries, the BM25 contribution to hybrid would
  likely matter more.
- *What does a stricter judge prompt produce?* If the
  faithfulness/relevance scores actually spread out under a
  better rubric, the comparison table gains additional ranking
  signal and the per-config differences sharpen.

## 14. Cross-client name leakage in generated answers — known issue, deferred fix

**Context.** During hand-verification of the CPO questions, the Q2
answer ("What measures do you have in place to ensure data security
and user privacy?") contained the sentence: *"The agreement to be
concluded between Utiq and Reach addresses the engagement of
processors."* — where "Reach" is a past client whose RFI is in the
corpus. The current question wasn't from Reach. The generator
faithfully copied a past Q&A pair into its synthesis, and the past
Q&A pair named the past client verbatim.

This is a real safety/privacy issue. Surfacing one client's name in
a response drafted for a different client is, depending on context:
- a confidentiality concern (does Reach want it known they ran a
  DPIA with this vendor?),
- a professionalism concern (it reads as a paste from another
  proposal),
- a basic awkwardness ("we work with Reach" delivered to a client
  who is not Reach).

**Why the eval missed it.** The LLM judge scored that exact answer
at faithfulness=5.0 because it WAS faithful to the retrieved
context — the cross-client name came directly from the past Q&A
pair the judge was comparing against. From the judge's defined
criteria (faithfulness, relevance, completeness) the answer is
correct. The leakage isn't in any of those dimensions.

This is the meta-lesson: a metric measures what its definition
says, and nothing more. Faithfulness-to-context cannot catch a
problem that is *in* the context. The eval framework is sound
inside its scope; the scope just doesn't include cross-tenant
content sanitisation. The hand-verification path (entry 12)
catches it.

**Design options for the eventual fix.** Not implemented today;
recording for whoever picks this up.

- *Prompt-level guard.* Add to the generation system prompt:
  "The retrieved Q&A pairs come from past clients. Do not name
  any specific client in your response. If you need to reference
  one, write 'a similar client' or omit the reference." Cheap to
  try, mostly effective for compliant LLMs, but trust-but-verify.
- *Post-generation redaction.* After Mistral produces the answer,
  run a regex / NER pass over it that replaces any known
  past-client name (we have these in `config_rfi_*.json` already
  — every config's `client` field) with `[a past client]` or
  similar. Deterministic, auditable.
- *Ingest-time sanitisation.* Replace client names in the
  ANSWER text before embedding. Cleanest semantically (the
  vector store never holds the leakable text), most invasive
  (loses original phrasing if you ever need to show the raw
  chunk to a human). Probably wrong choice — the verbose
  provenance display we built explicitly wants to show the raw
  past Q&A text.

The likely production answer is the first two combined:
prompt-level guard for the common case, regex/NER post-pass for
defence-in-depth.

**Also worth implementing alongside:** a `--client X` flag on
`query_rfi.py` that the user specifies (e.g. `--client "BBC"`).
The generator's prompt then explicitly includes "you are drafting
for BBC; do not name other clients" — making the redaction
target-aware rather than generic.

**What it teaches.**

When building a RAG system over a *private, multi-tenant corpus*,
cross-tenant content leakage is a first-class safety concern that
needs its own design + eval. Automated metrics scoped to
faithfulness/relevance/completeness will not catch it — they're
optimising for the wrong thing. Hand-verification by a domain
expert catches it because the domain expert reads the answer
with an implicit "would I be embarrassed to send this?" check.
The lesson is to encode that check explicitly: either as a
generation-prompt constraint, a post-processing pass, or both,
AND to add an eval metric ("answer mentions a non-target client
name: yes/no") that the formal eval can score on every config.

Spec Decision 7 mentioned tenant isolation in passing as a
"metadata filtering" pattern — filter the retrieval to a tenant's
own chunks. That's the right primitive for the *retrieval* side
(only retrieve your own past answers when querying for yourself).
But this RFI use case is the opposite: retrieve across all past
answers to learn from them, then generate a fresh answer for a
new client. The tenant boundary moves from retrieval to
generation. Both deserve eval coverage.


## 15. Pre-UI restructure: pipeline scripts → `pipeline/` package + distributed CLAUDE.md

**Context.** The pipeline shipped as six flat scripts at repo
root: `profile_excel.py`, `review_rfi_chunks.py`, `ingest_rfi.py`,
`query_rfi.py`, `eval_rfi.py`, plus the shared `mistral_helpers.py`
and the `loaders/` + `models/` packages. That layout worked fine
for the script-based pipeline. With the UI layer about to be added
(FastAPI + React per SPEC_UI.md), three problems surfaced:

1. **The api/services/ files want to import pipeline logic, not
   subprocess-shell it.** Streaming SSE events from a subprocess
   would mean parsing the child's stdout — fragile and tied to log
   format. Importing the functions lets the FastAPI process hold
   warm imports across requests and yield events directly.
   Subprocess-shelling was also the only honest option while the
   pipeline lived as root-level scripts, because *importing* a root
   script triggers its module-level argparse + side effects.

2. **The `pipeline` Docker Compose service name was about to
   collide with a proposed `pipeline` Python package name.** The
   double-pipeline (`docker compose run --rm pipeline python -m
   pipeline.profile`) reads as the same word twice with no signal
   about which one is the service and which the package.

3. **A single root CLAUDE.md was about to grow UI-layer
   conventions** (SSE event format, session.py contract, shadcn
   rules, useSSE hook contract, cross-tenant leakage handling in
   answer cards) that aren't relevant when working on pipeline
   improvements — and vice versa. Claude Code loads nested
   CLAUDE.md files automatically, so layer-specific guidance
   belongs in the layer's own subtree.

**What we did, in eleven commits on `feat/ui`.**

Commits 1–8 moved pipeline code into a `pipeline/` package, one
file per commit, each verified before the next:

- `mistral_helpers.py` → `pipeline/mistral_helpers.py`
- `loaders/` → `pipeline/loaders/`
- `models/` → `pipeline/models/`
- `profile_excel.py` → `pipeline/profile.py`
- `review_rfi_chunks.py` → `pipeline/review_chunks.py`
- `ingest_rfi.py` → `pipeline/ingest.py`
- `query_rfi.py` → `pipeline/query.py`
- `eval_rfi.py` → `pipeline/evaluate.py`

The `_rfi` suffix dropped because the package name already carries
the domain. `eval` became `evaluate` because `pipeline.eval` reads
awkwardly given the primary meaning of `eval` in Python — same
naming-trap rule that lives in root CLAUDE.md.

Commit 9 renamed the Docker Compose service `pipeline` → `cli`
and rewrote every invocation in the README + spec docs from
`docker compose run --rm pipeline python <script>.py` to
`docker compose run --rm cli python -m pipeline.<module>`. The
`-m` form is what makes both contracts work: argparse stays
intact for CLI users, and the module can still be imported by
external code without triggering CLI behaviour (because the
argparse calls live inside the `if __name__ == "__main__":` block).

Commit 10 split CLAUDE.md. Root keeps cross-cutting conventions
(privacy, Docker, Mistral SDK, ChromaDB, code style, naming
traps, branch discipline, active memory). `pipeline/CLAUDE.md`
captures the dual contract (CLI + importable), the "files copied
unchanged from a sibling learning project" list (moved here with
updated paths), Excel-specific conventions, and checkpoint
discipline. `api/CLAUDE.md` and `frontend/CLAUDE.md` are
deliberately deferred to the SPEC_UI steps that create those
directories — empty placeholders would invite drift.

**Alternatives rejected.**

- *Keep scripts at root, import them by name from api/services.*
  Possible — Python's CWD-on-sys.path means `from profile_excel
  import propose_mapping` would have worked. Rejected for two
  reasons: (a) the docker compose service / package name collision
  still bites because the package would be implicit (the script
  directory) rather than explicit (a named package), and (b) any
  module-level side effect in a root script — a `print()`, a
  `argparse.parse_args()` outside `__main__`, a `chromadb.PersistentClient(...)`
  — would fire on import and break the SSE flow.

- *Symlink the old script names to the new module paths.* Rejected
  per the CLAUDE.md "no backwards-compatibility hacks" rule. The
  only callers were the README and the spec docs; updating them is
  one commit. A symlink would be permanent dead weight.

- *Single root CLAUDE.md grown to cover both layers.* Rejected
  because every Claude Code session would load both sets of
  conventions regardless of which subtree it's working in.
  Pipeline-only sessions don't want SSE rules; UI-only sessions
  don't want the openpyxl `data_only=True` rule. Co-locating
  guidance with code keeps each session focused.

- *Make `pipeline/__init__.py` re-export every public function.*
  Rejected. Each module has its own ergonomic CLI surface and its
  own set of importable names — a flat `from pipeline import
  profile_propose_mapping` re-export would either duplicate that
  surface or hide it behind a leaky alias. Importers reach into
  modules by name (`from pipeline.profile import <fn>`) which
  keeps the public surface honest and forces module boundaries
  to mean something.

**What it teaches.**

Two things, both about *timing*:

1. **Restructure before the dependency, not after.** The
   restructure cost ~30 minutes and 11 commits when nothing
   depended on the old layout except docs. Doing it after the
   FastAPI scaffold was already importing from root scripts
   would have rippled through api/services/ tests, half-written
   SSE handlers, and any frontend code that had baked in the old
   shape. The right time to move files is before the next layer
   touches them.

2. **Distribute conventions before they collide.** A single
   CLAUDE.md grew naturally during pipeline work because there
   was only one layer; the conventions in it WERE the conventions
   of the layer. Adding a second layer made the file fork
   internally even before any UI code landed (notice the
   "pipeline scripts stay unchanged" rule from the original root
   CLAUDE.md — that's pipeline-specific guidance wedged into a
   nominally cross-cutting file because there was nowhere else
   for it to go). Splitting before the new layer arrives keeps
   the layer-specific rules where they belong from day one,
   rather than relocating them later when they've grown sticky.

The restructure is **organisational only** — no pipeline behaviour
changed. The production recommendation from entry 13
(`rfi_separated_cosine` + semantic + crossencoder + top-k=3)
remains valid against the renamed modules: every `--help` smoke
test confirmed identical CLI surface, and the eval framework
would produce identical numbers if rerun against the same
ChromaDB collections.


## 16. UI Step 1 — FastAPI backend scaffold + session management

**SPEC_UI Step 1 deliverable.** Stand up `api/` with FastAPI,
stub the three workflow routers (sessions / ingest / answer),
implement `api/session.py` (create, get-or-404, TTL cleanup),
add `backend` as a second Docker Compose service, and write
`api/CLAUDE.md` to capture the backend conventions. No real
behaviour yet — Steps 2–5 fill in the routers.

**Decisions made in code, with the load-bearing reasoning.**

*Lifespan context manager, not `@app.on_event`.* FastAPI deprecated
the `@app.on_event("startup")` pattern in 0.93. The lifespan
asynccontextmanager keeps startup + shutdown in one place and is
what FastAPI will still support five versions from now. The
SPEC_UI snippet showed the older form; modernised here without
changing the contract.

*Filesystem-backed sessions, not a database.* A single-purpose
internal tool with predictable per-session state does not need
the durability, query, or migration story a database provides.
A `tmp/{uuid}/` directory per session is auditable (one
inspectable folder per workflow), debuggable (`ls` shows what was
written), migration-free, and operationally cheap to clean
(delete the tree). Multi-user concurrency is trivially safe
because no two sessions share a file.

*Startup-only TTL sweep, not a midnight timer.* The spec proposed
"on startup AND at midnight" — the second half implies an asyncio
background task. Dropped in favour of startup-only because an
internal tool typically restarts daily anyway, getting the same
effect for zero infrastructure cost. If sessions ever accumulate
in practice, promote this to a background task; until evidence
of need, the simpler shape wins. The TTL is 24 hours: generous
for "user walked away, came back tomorrow" without indefinitely
holding uploaded RFIs that may contain real client data.

*Backend is a separate Docker Compose service, not a second
command on `cli`.* They share the image (same Dockerfile) but
want different runtime ergonomics: `cli` is interactive and
short-lived for one-off pipeline invocations, `backend` is
long-running on port 8000 with `uvicorn --reload`. Keeping them
as separate services means `docker compose up backend` does the
right thing for the UI while `docker compose run --rm cli` stays
unchanged for CLI work. Both bind-mount the whole project
directory, so file edits on the host propagate to both.

*The session_id is a capability token, not an auth token.*
Documented prominently in `api/CLAUDE.md`. UUID4 is unguessable
enough to disambiguate concurrent users behind an existing
reverse proxy / SSO layer, but it does not authenticate anyone
or grant access to corpus data. The intended deployment puts
real auth in front; sessions are infrastructure-free
per-tab state isolation, not security. Mixing these would invite
the wrong threat model.

*Import pipeline functions, never subprocess-shell them.* This
is the load-bearing reason the restructure (entry 15) happened
when it did. The api/CLAUDE.md spells out both sides of the
contract: backend services do
`from pipeline.profile import <fn>`; the pipeline modules
guarantee module-level side-effect freedom (no argparse at
import time, no chromadb client at module scope). Subprocess
output parsing would be fragile and would re-pay the cold-start
cost of importing chromadb + sentence-transformers on every call.

**Verification.** `docker compose up backend` starts uvicorn
on :8000. `GET /healthz` returns `{"ok": true}`.
`GET /api/sessions`, `GET /api/ingest`, `GET /api/answer` each
return their `{"status": "stub"}` placeholder.
`POST /api/sessions` returns a stub too. Cleanup logging
appears in the startup output. (Real session creation +
file upload arrive in Step 2.)

**What it teaches.**

Two things that will pay off across the rest of the UI build:

1. *Pick the modern FastAPI surface now, even when the spec
   shows the older one.* Lifespan over `on_event`; SSE via
   `StreamingResponse` with explicit `X-Accel-Buffering: no`;
   typed Pydantic responses where applicable; async route
   handlers throughout. The spec is a contract for *what*; the
   framework's current best practice is the contract for *how*.

2. *Co-locate layer conventions with layer code on day one.*
   `api/CLAUDE.md` exists from the first commit of the api/
   directory, capturing the "import not subprocess" rule, the
   SSE event format, the session-is-not-an-auth-token warning,
   and the cross-tenant leakage handling requirement. Putting
   these in a per-layer CLAUDE.md before any business logic
   exists means every future api/ change loads them
   automatically. Doing it later — once five service files
   already exist — means relocating sticky guidance and
   re-arguing what should have been settled at scaffold time.


## 17. UI Step 2 — Ingest router upload + profile SSE

**SPEC_UI Step 2 deliverable.** First real workflow code in the
UI. `POST /api/ingest/upload` saves the Excel under
`tmp/{session_id}/upload.xlsx` and returns a row-count estimate.
`GET /api/ingest/profile` streams the profiler as Server-Sent
Events. The profile service wraps `pipeline.profile.*` as an
async generator. This is the first time the import-not-subprocess
rule (entry 16) actually carries load.

**Decisions made in code, with the load-bearing reasoning.**

*Every blocking pipeline call goes through `asyncio.to_thread`.*
`pipeline.profile.*` is synchronous: openpyxl I/O plus a Mistral
HTTP round-trip. Calling those directly inside an `async def`
endpoint would block the FastAPI event loop and pause every other
in-flight request for the duration. `asyncio.to_thread` schedules
the synchronous work on the default thread executor; the event
loop stays responsive and concurrent sessions remain isolated.
The pipeline modules themselves do not need to be rewritten as
async — that would be a deep refactor of working code for a
performance characteristic that thread offloading gives us
cheaply. The price is one extra import (`asyncio`) and a thin
`_to_thread` helper.

*The profile flow is broken into discrete `step` events fired
AFTER each phase completes, not before.* A "starting phase X"
event tells the user what is about to happen but cannot report
what actually came out of it. A "completed phase X with result Y"
event reports actionable information — "Sheet selected: Sheet1
(highest question-mark count, 4 cells)". The narrative emerges
from real boundaries (sheet picked, header detected, columns
profiled, LLM responded, validated) instead of cosmetic
progress messages. One exception: the Mistral call is announced
beforehand ("Calling Mistral...") because it is the longest wait
in the flow and silence there would feel like a stall.

*The proposal is persisted to `profile.json` BEFORE the proposal
event is yielded.* If the SSE connection drops between yield and
the user clicking Approve, the proposal still survives on disk
and the next Step-3 POST can read it. Yielding first and writing
after would create a window where the user sees the proposal but
the backend has no record of it. Filesystem-first is the cheap
side of the race — and it composes with the "filesystem is the
state store" decision from entry 16.

*Uploads always land at the fixed filename `upload.xlsx`, not the
original name.* Three reasons: (1) the session directory is the
only state location, so the profiler service does not need to be
told which file in the directory to open; (2) each session holds
one workflow, so re-uploading replaces; (3) original filenames
carry client names ("Utiq_Publicis RFI.xlsx") that we would
rather not surface in any future filesystem listing leaked
through an error message. The original filename is still
returned in the upload response so the frontend can display it
back to the user — we just don't use it on disk.

*Exceptions inside the SSE generator become typed error events,
not bubbled exceptions.* An exception thrown inside an async
generator that backs a `StreamingResponse` becomes an opaque 500
on the wire — the SSE client sees the stream close with no
useful message. A catch-all that yields
`{"type": "error", "data": ...}` preserves the per-event protocol
all the way to the browser, where the frontend can render
something meaningful. The catch is at the outermost level of the
generator; specific failures (validation issues, missing upload
file) yield more structured error events earlier.

*Validation failures terminate the stream, do not stream the
proposal.* `pipeline.profile.validate_proposal` returns a list of
issues; if non-empty, the LLM produced an invalid mapping (two
columns labelled `question`, an invalid metadata role name, etc.).
The SSE service yields a single error event with `issues: [...]`
and returns, rather than yielding the malformed proposal. The
human approval flow in Step 3 should never see a proposal the
validator rejected — that asymmetry is what makes the validator
load-bearing rather than ceremonial (entry 4).

**Verification.** Workflow ran end-to-end against
`data/Utiq_Publicis RFI.xlsx`:

  POST /api/sessions                    -> {"session_id": "<uuid>"}
  POST /api/ingest/upload?session_id=.. -> {"detected_rows": 60, ...}
  GET  /api/ingest/profile?session_id=  -> 8 streamed events:
       step "File opened — 1 sheet(s) detected"
       step "Sheet selected: \"Sheet1\" — highest question-mark count"
       step "Header row: 12 — row 12 contains header label 'UTIQ response'"
       step "Columns profiled: 3 columns, 48 data rows"
       step "Calling Mistral for column→role mapping..."
       step "LLM recommendation received"
       step "Proposal validated"
       proposal {B=question, C=answer, A=ignore, reasoning=...}
       done

`tmp/{sid}/upload.xlsx` matched the source byte-for-byte (24 928
bytes). `tmp/{sid}/profile.json` was written (1 473 bytes) with
the full mapping payload.

**What it teaches.**

The pipeline restructure (entry 15) is what made this Step easy.
If the pipeline still lived as flat scripts at root, the
profiler service would have had to either subprocess-shell out
to `python -m pipeline.profile` (parsing stdout for events) or
copy-paste the profiler's Phase 1/2/3 functions into the service
file. Both are bad — the first is fragile and slow, the second
double-maintains business logic. Because the restructure
guaranteed that `pipeline.profile.auto_detect_header_row` etc.
are importable without side effects, the service is a thin
async wrapper that adds nothing but the SSE event shape and the
thread-offload.

Net effect: the api/services/profiler.py file is ~50 lines of
real logic plus comments. That is the right size for a wrapper.
If it were 500 lines, that would be a signal the pipeline
boundary was drawn in the wrong place — and the restructure
would have been wasted. The fact that it isn't is the test that
the pipeline package was carved at the right joint.


## 18. UI Step 3 — Approve + ingest SSE

**SPEC_UI Step 3 deliverable.** `POST /api/ingest/approve` reads
the profile, applies the user's client/date edits, persists the
config to both the session dir AND the durable repo-root
location, copies the upload into `data/`, and streams ingest
progress as Server-Sent Events. The ingester service composes
existing `pipeline.ingest` helpers — only the per-batch yield
loop is new.

**Decisions made in code, with the load-bearing reasoning.**

*Approve is a POST that returns an SSE stream, not a POST-then-GET
split.* The spec calls for the approve endpoint to commit the
config edits AND stream progress in one request. EventSource (the
browser's native SSE client) only supports GET, so the frontend
will use the `fetch` + `ReadableStream` pattern instead. The
alternative — POST commits + GET streams — would mean two round
trips and a race window where the GET could begin before the
commit lands. One request keeps the contract atomic.

*Two persisted copies of the config: session-local + repo-root.*
The session gets `tmp/{sid}/config.json` because the session is
the auditable record of one workflow (you can `ls` it and see
upload → profile → config in one place). The repo root gets
`config_rfi_<slug>.json` because the CLI ingest's
`load_all_rows()` scans `glob("config_rfi_*.json")` — for a
future `python -m pipeline.ingest --reset` to rebuild ChromaDB
from disk, the durable config has to live where the CLI looks.
Drift between the two would mean the CLI re-ingest produces
different chunks than the UI just produced, so they are written
from the same JSON string in one statement.

*The Excel is COPIED into `data/<original_filename>`, not moved.*
The session upload stays in `tmp/{sid}/upload.xlsx` until the
TTL sweep. If the approve fails midway (Mistral 500, ChromaDB
write timeout), the user can re-approve without re-uploading.
Move-not-copy would have made the upload non-reusable.

*Original filename is persisted as a sidecar at upload time, not
passed back through approve.* `tmp/{sid}/original_filename` is
written by `POST /api/ingest/upload` and read by the ingester.
The frontend never has to carry that string across the
upload → profile → approve sequence. This is the
"backend remembers, frontend kicks the workflow" pattern from
api/CLAUDE.md: state lives on disk between requests, not in the
client. Bonus: the same filename ends up in profile.json output,
config.json, and `config_rfi_<slug>.json` without three copies
of "remember to pass this back" code on the frontend.

*Reuse `pipeline.ingest` helpers; re-implement only the embed loop.*
`COLLECTIONS`, `BATCH_SIZE`, `embed_batch`, `chunk_id`,
`sanitize_metadata`, the checkpoint accessors — all imported
verbatim. The one piece we cannot reuse is `ingest_file` because
it `print()`s to stdout and does not yield. So the service has
its own embed loop that wraps `embed_batch` + `collection.add`
in `asyncio.to_thread` and yields `{batch: i, total: N}` events
between batches. The duplication is ~12 lines and the
alternative (refactoring `ingest_file` to be a generator)
would change the CLI's behaviour for no other reason than UI
convenience — the kind of pipeline-touching change
`pipeline/CLAUDE.md` forbids.

*Column roles are NOT editable in the UI; only client/date are.*
SPEC_UI's Ingest wireframe shows only client and date as
editable fields, and the approve body's Pydantic schema reflects
that (`session_id`, optional `client`, optional `date` — nothing
else). If a column was mis-classified by the LLM, the user
rejects the proposal and re-profiles. Two reasons: (a) editing a
column role in the UI without re-validating would be a foot-gun
— the validator's "exactly one question column, exactly one
answer column" invariant can be silently violated by mid-flight
edits; (b) keeping the column-role decision in the LLM/validator
layer means the production recommendation from entry 13 stays
reproducible — every UI-ingested file follows the same path the
CLI-ingested files did.

*Checkpoint discipline carries over from the CLI.* The UI ingest
uses the SAME `outputs/.ingest_checkpoint.json` the CLI does.
After a UI ingest succeeds, the four (collection, file) pairs
are recorded. A subsequent CLI run sees them and skips.
Inversely, a file already in the checkpoint (e.g. one of the
production-ingested RFIs) is recognised when re-uploaded via UI
and yields a `complete` event with `note: already in checkpoint
— skipped`. This is what makes the UI ingest and CLI ingest
operationally interchangeable — they read and write the same
durable state.

**Verification.** End-to-end ran against
`data/Utiq_Publicis RFI.xlsx`, which is already in the
production checkpoint:

  POST /api/sessions                       -> {session_id: <uuid>}
  POST /api/ingest/upload?session_id=...   -> {detected_rows: 60, ...}
       (sidecar written: tmp/{sid}/original_filename = "Utiq_Publicis RFI.xlsx")
  GET  /api/ingest/profile?session_id=...  -> 8 events ending in proposal
  POST /api/ingest/approve                 -> 9 SSE events:
       collection rfi_combined_cosine
       complete   {collection: rfi_combined_cosine, chunks: 0, note: "skipped"}
       collection rfi_combined_l2
       complete   {collection: rfi_combined_l2, chunks: 0, note: "skipped"}
       collection rfi_separated_cosine
       complete   {collection: rfi_separated_cosine, chunks: 0, note: "skipped"}
       collection rfi_separated_l2
       complete   {collection: rfi_separated_l2, chunks: 0, note: "skipped"}
       done       {total_chunks: 0, corpus_size: 1646}

Final corpus size = 1646 (matches the pre-test corpus — no
duplicate writes, no drift). Both `tmp/{sid}/config.json` and
`config_rfi_utiq_publicis_rfi.json` at root are byte-identical
and reflect the user-edited values (`client: "Publicis"`,
`date: "2024"`) overriding the LLM's null inferences.

The actual embedding code path is the same code the CLI uses
to produce the 1 646-chunk corpus the production eval validates.
Verifying its event-streaming wrapper against a not-yet-ingested
file is deferred until a real new RFI is added — or by
manually clearing one (collection, file) entry from the
checkpoint and re-running. Not part of this Step's smoke test
because the embed path itself has no UI-specific logic to
exercise.

**What it teaches.**

The dual-persistence pattern (session-local config.json +
repo-root config_rfi_<slug>.json from one write) is the
load-bearing trick that lets the UI and CLI share state without
either mode needing to know the other exists. The UI doesn't
have to learn the CLI's checkpoint layout; the CLI doesn't have
to learn that some sessions originated from the web. They both
write/read the same canonical artefacts. If, much later, we add
a third entry point (e.g. a CI job that ingests a new RFI from
S3), it follows the same shape: write `config_rfi_<slug>.json`,
copy the file into `data/`, mark the checkpoint. The trio is
the corpus contract.

This generalises: when wrapping a CLI as a UI, the UI's job is
not to *replace* the CLI's persistence model but to *participate*
in it. Anything the UI persists that the CLI ignores would
either drift or be wasted work. Anything the CLI persists that
the UI ignores would break re-runs.


## 19. UI Step 4 — Answer workflow upload + per-question SSE

**SPEC_UI Step 4 deliverable.** The Answer workflow: a fresh client
RFI arrives, the backend extracts its questions, then streams
generated answers one at a time, each carrying full retrieval
provenance. This is the workflow the CPO singled out as
"valuable specifically because the source attribution stays
visible" (LEARNING_NOTES entry 12). Preserving that visibility
in the UI was the explicit ask from feedback memory; the answer
event payload was designed around it.

**Decisions made in code, with the load-bearing reasoning.**

*Use the production config from entry 13, not the older
recommendation in the spec.* SPEC_UI Step 4's prompt was written
before the eval finalised and specified
`rfi_separated_cosine + hybrid + crossencoder + top-k=3`. The
eval that ran later (entry 13) determined semantic retrieval
beat hybrid on this small/paraphrase-rich corpus. The UI default
follows entry 13 (`semantic`, not `hybrid`) — the spec describes
the workflow shape; the eval describes which config wins
empirically. Disagreement is resolved in favour of the empirical
finding, with this note documenting why.

*Question extraction is heuristic-first, LLM-fallback.* The
ingest workflow's profile step is full Mistral+human; the answer
workflow only needs ONE field (the question column letter). The
heuristic from `pipeline.profile.profile_sheet` catches the easy
cases instantly. When it returns zero candidates — which happens
when the question column has prose questions without "?" and a
non-obvious header like "Company Overview" — we fall back to a
Mistral mapping call (same `request_mapping` the ingester uses)
and read `column_roles[X] == "question"`. The fast path stays
fast for files that fit the heuristic; the slow path costs one
Mistral round-trip (~1-3s) for harder files. The user sees which
method was used in the persisted `answer_questions.json`'s
`detection_method` field, so column detection is auditable.

*The answer event payload carries the full retrieval trace,
not a summary.* Each `answer` event includes a `sources` list
where every entry has rank, source_file, pair_id, section,
client, score, score_type, question_text, AND answer_text. The
AnswerCard in the frontend gets everything needed to render
"this answer was drawn from these three past Q&A pairs, each
showing the original question and the original answer". No
"summary score" or "abbreviated source list" hides the
provenance from the reviewer. This is the
verbose-provenance-is-the-default rule (active memory
`feedback-show-provenance`) expressed at the event-payload
level — make it impossible to display less than the full trace.

*Refusal is a first-class flag, not a substring match in the
frontend.* The generator's refusal sentinel is the exact string
"I cannot find this in our corpus." (set in the prompt; see
LEARNING_NOTES entry 10). The service compares
`answer_text.strip() == REFUSAL_TEXT` and sets `refused: true`
on the event. The frontend can pattern-match the string instead,
but every consumer would be re-deriving the same predicate.
Expressing it once at the boundary keeps the contract clean —
if the refusal phrasing ever changes, only this service needs
to follow.

*Cross-tenant client mentions are flagged per answer.* From
LEARNING_NOTES entry 14 and active memory
`feedback-cross-tenant-leakage`: generated answers can name
past clients verbatim because retrieved chunks do. The pipeline-
layer fix (prompt guard + post-redaction) is not implemented yet.
Until it is, the UI surfaces the risk per answer: every known
client name (collected from `config_rfi_*.json` files at repo
root) is matched against the generated answer text with a word-
boundary regex, and any hits are listed under
`mentioned_clients`. The frontend renders this as a visible
warning badge on the AnswerCard. This makes the
"do not ship a send-directly-to-client path" rule from active
memory enforceable: every answer that names a past client is
marked, the reviewer sees the mark before approving, and the
human review gate stays load-bearing rather than ceremonial.

*Word-boundary matching, not substring.* A naive substring
match would flag "Reach" inside "outreach" or "research" — false
positives that train the user to ignore the warning. The match
uses `\bNAME\b` (case-insensitive) so "Reach" matches only when
it is a whole word. Trade-off: client names with embedded spaces
("Bank of Foo") will match across whitespace correctly, but a
client name that overlaps with a common English word will still
hit false positives on accidental references. Acceptable — false
positives lean toward over-warning, which is the safe side.

*Synchronous Mistral calls run on the thread pool, not the event
loop.* Same pattern as the profiler service. Each
`_answer_one_question` call invokes `retrieve_semantic`,
`rerank_crossencoder`, `fetch_paired_answers`, and
`generate_answer` — every one of them blocking. Wrapping the
function in `asyncio.to_thread` lets multiple tab-level sessions
(or, in single-user dev, the SSE producer and the FastAPI event
loop) make progress concurrently.

*Upload returns synchronously; process is SSE.* Question
extraction is fast (no Mistral, or one mapping call at most);
the user wants to see "yes, 28 questions found" before clicking
"Start answering". So `POST /api/answer/upload` returns a normal
JSON response with the question count + a 3-question preview.
The slow per-question generation is the GET `/api/answer/process`
endpoint, which IS SSE because it can take minutes for a long
RFI.

**Verification.** Workflow ran end-to-end against
`data/Utiq_Publicis RFI.xlsx` (28 questions in column B,
heuristic-detected as Company Overview → LLM fallback identifies
column B as `question`):

  POST /api/sessions               -> session_id
  POST /api/answer/upload          -> {question_count: 28,
                                       question_column: "B",
                                       question_column_header: "Company Overview",
                                       questions_preview: [3 questions]}
       answer_questions.json persisted with detection_method
       = "llm-fallback".
  GET  /api/answer/process         -> SSE stream:
       progress index=1 ... total=28 question_text="..."
       answer   index=1 question="..." answer="..."
                 sources=[3 chunks with score+pair_id+source_file
                          +question_text+answer_text per chunk]
                 confidence=<top score>
                 pair_ids=[3 pair ids]
                 mentioned_clients=[]
       (repeated 28x)
       done     {answered: N, refused: M, total: 28}

  answers.json persisted (28 entries with full provenance).

**What it teaches.**

The CPO feedback the project is built around — "I value being
able to see where each answer came from" — could have been
implemented as a generic confidence number on each answer.
That would be the easy default and it would have been wrong.
What makes the provenance load-bearing is that it's *the same
shape as the underlying retrieval* — the same chunks, the same
scores, the same source filenames the CLI prints to stdout. The
UI is not summarising the retrieval; it is *displaying it
directly*. Anything less would be a wrapper that hides the
thing the user actually cares about.

The general lesson: when the *visibility* of a system's
internals is a feature (not an implementation detail), the
wrapper layer must transmit those internals end-to-end without
remixing or summarising. The summary is the consumer's job, not
the wrapper's. The wrapper's job is to make the internals
*available*.


## 20. UI Step 5 — Excel exporter (filled RFI download)

**SPEC_UI Step 5 deliverable.** The user has reviewed the
streamed answers, edited some, skipped some, and clicks
"Download filled RFI". The backend opens the original upload,
appends three columns (Suggested Answer / Source RFIs /
Confidence), and streams the file back as a `.xlsx` download.

**Decisions made in code, with the load-bearing reasoning.**

*Open the workbook with `data_only=False` (the openpyxl default).*
The profiler uses `data_only=True` to read formula *results* for
column classification. The exporter does NOT read formula cells
— it only appends NEW columns past the last existing one and
writes their header + per-row text. Opening with `data_only=False`
preserves formulas, formatting, merged cells, validation rules,
and conditional formatting in the existing columns. We do not
touch them. The user's original RFI looks unchanged except for
the three new columns at the end.

What openpyxl cannot preserve through a round-trip: VBA macros
(.xlsm files re-saved as .xlsx drop them), embedded charts in
certain shapes, ActiveX controls. RFI files are typically plain
Q&A tables so this is a non-issue in practice. Documented as a
caveat in the service docstring; if a future RFI ships with
critical macros, the workaround is to copy the new columns into
the original file by hand.

*Two-step edit-then-export, not a POST that returns the file.*
SPEC_UI Step 9's ExportButton specifies "POST edits to backend
then GET /api/answer/export, trigger browser download". The GET
download has two practical advantages over a POST that returns
the file:

  - Browsers handle GET downloads cleanly (history, retry, file
    name from Content-Disposition).
  - The exporter reads all inputs from disk
    (`answers.json` with `_status` flags set by `/edit`), so
    the export is idempotent — re-running it produces the same
    output without the frontend re-sending the edits.

The edit step persists `_status` per answer:

  - "accepted" (default): write generated answer + sources + confidence
  - "edited": answer text replaced, refused flag cleared
  - "skipped": all three new columns set to None (blank in Excel)

After `/edit`, `answers.json` on disk is the single source of
truth. The export reads it once. Re-running `/export` without
intervening edits returns the same bytes (modulo openpyxl's
internal ordering — close enough for hashing purposes).

*Refusal text lands in the cell, not blank; skip lands blank.*
When the generator refuses ("I cannot find this in our
corpus."), that *is* the answer the system produced — it's
load-bearing information for the reviewer ("we don't have
anything to say here, you'll need to draft this manually").
Leaving the cell blank would hide the refusal and make the row
look like a system miss. Writing the refusal text into Suggested
Answer keeps the audit trail intact.

Skipped answers DO get a blank cell because the user has
actively rejected the suggestion — the absence of content is the
user's decision, not the system's silence. Refusal and skip are
semantically different and the export distinguishes them.

*Source RFI cell value is human-readable, pipe-separated.* The
spec format is "<source_file> row <N>" joined with " | ". Source
file is preserved verbatim from the chunk metadata
(`Utiq_Publicis RFI.xlsx`). Row is extracted from the pair_id
(`utiq_publicis_rfi_row_13` → `13`) with a regex against the
project-wide convention `<slug>_row_<N>` defined in
`pipeline.loaders.excel_loader`. If the regex misses, the full
pair_id is written rather than dropping attribution silently —
defensive against future pair_id format changes.

*Confidence is the top-chunk crossencoder score, rounded to 2dp.*
What lands in the cell is one float per row, e.g. `8.21`. The
underlying score is the rerank score of the highest-ranked chunk
that fed generation. A reviewer scanning the Confidence column
can sort it in Excel to triage low-confidence answers first.
Higher is better (crossencoder scoring).

*Download filename = `<original_stem>_answered.xlsx`.* The
original filename sidecar (`tmp/{sid}/original_filename`,
written by both ingest AND answer upload — fixed during this
step in the answer endpoint, which had been missing it) is read
to produce a download name like `Utiq_Publicis RFI_answered.xlsx`.
Without the sidecar, falls back to `output.xlsx`. FastAPI's
`FileResponse(..., filename=...)` sets the `Content-Disposition`
header so the browser saves under the right name automatically.

**Verification.** Full edit-then-export run against the previous
session's `tmp/{sid}/answers.json` (28 questions from
`Utiq_Publicis RFI.xlsx`):

  POST /api/answer/edit {overrides: {1: "EDITED: ..."}, skipped: [5, 10]}
       -> {"modified": 3}

  GET  /api/answer/export?session_id=...
       -> 200 OK, application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
       -> Content-Disposition: attachment; filename="Utiq_Publicis RFI_answered.xlsx"
       -> 18,755 bytes

  Opened in openpyxl:
       max_column = 6 (was 3 before export: A=ignore, B=Company
                       Overview, C=UTIQ response; new D=Suggested
                       Answer, E=Source RFIs, F=Confidence)
       Header row 12 carries the 3 new headers verbatim.
       Edited idx=1 row=13: cell D13 starts with "EDITED: Utiq is..."
       Skipped idx=5 row=21: D21/E21/F21 all blank.
       Skipped idx=10 row=30: D30/E30/F30 all blank.
       Accepted idx=2 row=15: confidence 8.21, sources pipe-list
                              including "Utiq_Publicis RFI.xlsx row 15".

**What it teaches.**

The exporter is small (~200 lines) because the hard work was done
upstream. Question extraction (Step 4) made `answers.json`
carry pair_id, source_file, confidence, and row alongside each
answer — the exporter is then a pure transform from that record
into Excel cells. If question extraction had only persisted
"answer text" without provenance, the exporter would have needed
to redo retrieval at export time to populate Source RFIs.

This is a recurring shape in this codebase: when each step
persists every piece of state the NEXT step might want, the next
step shrinks. The disk format (`profile.json` → `config.json` →
`answers.json` → `output.xlsx`) is the actual interface; every
step is a small function from one disk format to the next. This
is what lets the UI ingest and CLI ingest share state cleanly
(entry 18), and what made wrapping the pipeline for SSE possible
without rewriting the pipeline (entries 17/18/19).

If a future maintainer wonders "why is the answer.json shape so
verbose, can we trim it?" — the answer is no. The exporter (and
any future post-processing: redaction, format conversion, audit
trail emission) reads from it. Trimming it would push work back
into those downstream tools.


## 21. UI Step 6 — Frontend scaffold (Vite + TS + shadcn + Router)

**SPEC_UI Step 6 deliverable.** Stand up `frontend/` with Vite +
React + TypeScript, install shadcn primitives, wire React Router,
add a Dockerfile + compose service, and write `frontend/CLAUDE.md`
covering the layer-specific conventions. No business logic yet —
the three pages (Landing, Ingest, Answer) are stubs; Steps 7–9
fill them in.

**Decisions made in code, with the load-bearing reasoning.**

*Vite proxy for /api, not CORS on FastAPI.* The browser fetches
`/api/...` from `localhost:3000`; Vite (running inside the
frontend container) proxies those requests to `http://backend:8000`.
This works identically in production behind any reverse proxy
that forwards `/api` to the FastAPI port. Hard-coding
`http://localhost:8000` in the frontend bundle would couple the
bundle to a specific deployment URL. CORS allowlists on FastAPI
would grow every time a new deployment URL appears. Relative
paths + a proxy keep both ends portable. The cost: ~10 lines of
Vite config.

*One unified `useSSE` hook covering GET and POST SSE.* The
backend has two SSE shapes: GET (profile, process — plain SSE,
EventSource works) and POST (approve — POST returning
text/event-stream, EventSource cannot consume). Rather than
shipping two hooks or a polyfill that fakes EventSource over
POST, the hook is a 100-line wrapper around `fetch` +
`ReadableStream.getReader()` for both cases. fetch handles GET
and POST identically; the response body's ReadableStream is the
same shape either way. Native APIs, no dependencies, debuggable
end-to-end. The hook surfaces `{events, status, error, start,
reset}` as React state so pages can either iterate `events` for
rendering or attach an `onEvent` callback for side effects.

*shadcn components written by hand, not via the interactive CLI.*
`npx shadcn@latest init` and `add` are interactive — they prompt
for framework, style, base color, paths. Running them in Docker
non-interactively is possible with flags but brittle across
shadcn CLI versions. The components themselves are public-domain
templates (~40-100 lines each, mostly Radix wrappers). Writing
the eight needed components (Button, Card, Progress, Badge,
Textarea, Input, Table, Dialog) by hand once is faster than
fighting CLI interaction in the container, and the
`components.json` config is present so future `shadcn add` works
when we need more.

*Frontend has its own image (node:20-alpine), bind-mount + anon
volume for node_modules.* The frontend toolchain shares zero
deps with the Python pipeline — separate image. The bind-mount
delivers Vite HMR (edit .tsx on host, browser reloads in <1s).
The anonymous volume on `/app/node_modules` is the canonical
trick that prevents the host's empty or platform-mismatched
node_modules from shadowing the container's at startup. Without
it, the first compose run after `docker compose build frontend`
would fail to find React.

*Pages never call `fetch()` or instantiate `EventSource`
directly.* All backend calls go through `src/lib/api.ts`; all
SSE consumption through `useSSE`. Pages import typed functions
and typed event types. This makes the type surface honest (one
place to follow when the backend shape changes) and creates the
seam future tests would need (mock api.ts, render the page).
The frontend/CLAUDE.md spells this out as a hard rule.

*Verbose provenance is mandated at the convention level, not the
component level.* `frontend/CLAUDE.md` declares that AnswerCard
must render every source chunk with source_file + row + score by
default — no hover-to-reveal, no "summary score" hiding the
trace. Active memory `feedback-show-provenance` and
LEARNING_NOTES entry 12 made this load-bearing; codifying it in
the layer's CLAUDE.md means future contributors don't re-discover
the requirement from feature requests. Same shape for the
cross-tenant-leakage warning: `mentioned_clients` non-empty →
visible warning Badge before the action buttons.

*localStorage for session_id, scoped per workflow.* Each
workflow stores its session_id in localStorage under
`rfi.ingest.session_id` / `rfi.answer.session_id`. On completion
or "start over", the key clears. The session_id is per-tab
capability state (see api/CLAUDE.md: NOT auth), so localStorage
is fine — there is no persistence requirement beyond surviving
a page reload, and the backend's 24h TTL bounds the worst case.
Cookies would work too but would interact with deployment-time
proxy/SSO and aren't worth the complexity for the same outcome.

**Verification.** `docker compose build frontend` succeeded
(image based on node:20-alpine, all npm deps resolved without
peer warnings). `docker compose up frontend` started Vite on
:3000 with HMR. Browser visits:

  http://localhost:3000/         -> Landing stub renders
  http://localhost:3000/ingest   -> Ingest stub renders
  http://localhost:3000/answer   -> Answer stub renders

All three routes render the header nav, the workflow card, and
the routing chrome with no console errors. Tailwind classes
apply (the cards have the shadcn border/shadow/spacing).
Imports from `@/components/ui/*` and `@/pages/*` resolve via
the TS path alias.

**What it teaches.**

The scaffold is ~25 files, but they're each small and they're
all *boundary* code: config that wires the toolchain, typed
wrappers, primitive components, CLAUDE.md conventions. Zero
business logic landed here. That separation is deliberate: by
the time Step 7 starts (Landing page content), the scaffold is
frozen and the work is purely composition. If the scaffold had
mixed in a Dropzone component for ingest or an AnswerCard for
answers, Step 6's commit would have been impossible to review —
"what's setup vs. what's the actual landing page?" Splitting
scaffold-only into its own commit makes the next steps land
cleanly as "feature-N on top of frozen scaffold".

The generalisation: when starting a new layer, the FIRST commit
should be 100% scaffold + conventions, ZERO business logic. The
next commits then have a stable surface to build on. Mixing
"set up the framework AND build the first feature" in a single
commit creates a commit that is too big to review and that
nobody can untangle later when one of the two halves needs to
change in isolation.


## 22. UI Step 7 — Landing page + corpus stats endpoint

**SPEC_UI Step 7 deliverable.** The first user-facing page. Two
shadcn Cards side-by-side (stack on mobile) — one for each
workflow — plus a footer that fetches `GET /api/corpus/stats`
and shows "N Q&A pairs across M source RFIs".

**Decisions made in code, with the load-bearing reasoning.**

*Read corpus stats from `rfi_combined_cosine`, not the
production-recommended `rfi_separated_cosine`.* Both collections
see every RFI ingested, but separated stores TWO chunks per Q&A
pair (one question, one answer). Reading distinct pair counts
from separated would require iterating metadata to deduplicate.
Combined stores ONE chunk per pair, so `collection.count()` IS
the pair count directly. The choice is for display only — query
routing still uses `rfi_separated_cosine`. Cheap operation,
clean number.

*Corpus stats endpoint fetches metadata for ALL chunks in a
single `collection.get(include=["metadatas"])`.* The intent
behind the endpoint is "give me a small JSON for the footer",
so the call is read-once-per-page-load. For the current 279-pair
corpus this returns ~4 KB of metadata in milliseconds. If the
corpus grows past ~50 k chunks (~6 MB of metadata roundtrip),
the right answer is to maintain a `source_files` index at
ingest time and read from that — not to make this endpoint
clever. The comment in the code names the threshold so a future
maintainer doesn't have to guess.

*StatsFooter renders three states: loading, error, populated.*
The Landing page is the application entry point. If
`/api/corpus/stats` returns 404 (no collection yet — a fresh
deployment with no ingests), the footer doesn't surface an
error toast; it shows a softer message "Corpus stats
unavailable — likely no RFIs ingested yet. Add your first RFI"
with a link to /ingest. The user landing on the page for the
first time gets pointed at the right next action; they do not
see a red error bar that suggests something is broken when in
fact the system is just empty.

*All backend file names are surfaced in a `title` attribute, not
listed prominently.* The footer's primary message is the two
numbers; the file list is secondary. Putting source filenames in
a tooltip (and a comma-joined truncated line below) lets a
reviewer who wants to verify "which files am I seeing" do so
without pushing names like "INTERNAL - Reach Customer facing
DPIA questions.xlsx" onto the page as visual noise. Filenames
carry sensitive client identifiers; the page intentionally does
not lead with them.

*The Get Started CTA on each card uses shadcn `Button asChild`
wrapping a `<Link>`.* That pattern propagates the Button styling
to the react-router-dom Link element without duplicating the
className surface. The alternative — onClick handlers calling
`navigate()` — would lose right-click "Open in new tab" and
break the browser's native link semantics. asChild is the
correct shadcn idiom for "I want a styled element that is
ALSO a router link".

**Verification.** The visual rendering of the Landing page in
a browser was NOT verified — this environment has no headless
browser to drive. Verification was performed at three lower
levels instead:

  1. `GET /api/corpus/stats` (backend direct):
     -> {total_pairs: 279, source_files: 4, files: [4 names]}
  2. `GET /api/corpus/stats` (via Vite proxy on :3000):
     -> identical payload (proxy passes through unchanged)
  3. `GET /src/pages/Landing.tsx` (Vite TS compile):
     -> 200 OK, 21 749 bytes of compiled JS, no errors in Vite
        log. Confirms the page's TypeScript compiles, all
        imports resolve (lucide-react ArrowRight/Database/
        FileSpreadsheet, getCorpusStats, the Card primitives,
        react-router-dom Link).

This caveat applies to every frontend Step from here on: the
project's CLAUDE.md notes "For UI changes, start the dev server
and use the feature in a browser before reporting the task as
complete" — we cannot fully honour that in this environment.
Where it matters (Steps 8 and 9 in particular, which have
substantial interactive flows), the user should manually open
http://localhost:3000 and click through the workflow before
treating a step as fully done.

**What it teaches.**

The Landing page is small (~110 lines of TSX) precisely because
the scaffold did the heavy lifting. Cards, Buttons, typed API
calls, Tailwind utilities — every primitive a Landing page
needs was already in place from Step 6. The page is pure
composition: pick the right primitives, give them content,
arrange them with Tailwind classes.

The deliberate consequence: when Step 8 adds the Ingest wizard,
its commit will be similarly composition-heavy and similarly
small in framework boilerplate. The scaffold pays itself back
on every page that lands on top of it. If we'd skipped the
scaffold step and built "framework + Landing" together, the
Landing commit would have been triple the size and reviewing
"is the page correct?" would have been entangled with "is the
framework correct?".


## 23. UI Step 8 — Ingest wizard (3-step Upload → Profile → Ingest)

**SPEC_UI Step 8 deliverable.** The first interactive flow in the
UI. A three-step wizard that uploads an Excel, streams the
profiler's discovery as a growing timeline, lets the user edit
the inferred client/date in a ProposalCard, then streams ingest
progress across the four collections with per-collection
progress bars.

**Decisions made in code, with the load-bearing reasoning.**

*Page-local state via `useState`, not a state-machine library.*
Three states (upload, profile, ingest), four transitions
(upload→profile, approve→ingest, reject→upload, finish→upload).
A reducer or XState would be overkill: the transitions are
colocated with the buttons that trigger them, and the state
reads top-to-bottom. If transitions grew to six or seven (or
needed guards), a reducer would start paying off. At three, it
would add framework noise for no clarity gain.

*The proposal is extracted from the events array via `useMemo`,
not held in a separate `useState`.* The proposal arrives as one
event among many in the profile SSE stream. Two ways to surface
it: write a setter inside an `onEvent` callback OR
`useMemo(() => events.find(...), [events])`. The memo form keeps
`events` as the single source of truth — when the stream resets,
the proposal disappears for free, no extra setter to reset.
It's also the same pattern used for the `done` event in Step 3.

*The four collection progress bars are rendered unconditionally,
not lazily as events arrive.* The list of four collection names
is statically known (the same constant the backend's
`pipeline.ingest.COLLECTIONS` exports). Listing them up front
means the layout doesn't reflow as events flow in — the
"waiting" state is just the default state for a collection
that hasn't yet seen a `collection` event. If the list weren't
fixed, the layout would jump every time a new collection
arrives — distracting on a slow run, and worse on flaky
connections.

*The "Reject & re-profile" button does a full session reset, not
a "go back" to the file picker with the upload intact.* The
profile.json on disk reflects what the profiler produced from
the current upload; if the user rejects, the LLM was wrong and
re-running the profiler against the same bytes would likely
produce the same wrong answer (temperature=0). The honest path
is "start over" — re-upload (which is cheap; the user already
has the file picked) and try again. Trying to be smart about
"keep the upload, drop just the proposal" would create a state
where tmp/{sid}/upload.xlsx exists with no profile.json, which
no other endpoint expects.

*Per-collection state is computed by replaying the events array
in a `useMemo`, not by mutating a `useRef`-held map on each
event.* The replay is O(events) on every render but events is
small (<20 entries per ingest) and React is fast. A
useRef-mutating approach would be slightly more efficient but
would mean the visible state and the events array could diverge
silently if a re-render is skipped. Replay is the safer
contract: "what you see is what was streamed".

*The `useSSE` hook is invoked twice — one instance per stream
(profile + ingest).* The hook holds events + status + error in
its own state; two streams require two instances. A single
hook with two start methods would tangle the two streams'
state into one object. Keeping them disjoint matches the
underlying reality (two independent backend connections).

*The done summary uses `corpus_size` from the SSE event, not a
post-stream refetch of `/api/corpus/stats`.* The backend's
ingest service yields `{total_chunks, corpus_size}` in the
final `done` event — the corpus size already reflects the
just-completed ingest. Refetching after the stream closes
would add a round trip and risk a race window where the post-
ingest stats are still being computed. The yielded value is
authoritative; use it.

**Verification.** Three TSX files compile via Vite without
errors:

  /src/components/StepTimeline.tsx     OK  9 540 bytes compiled
  /src/components/ProposalCard.tsx     OK 22 305 bytes compiled
  /src/pages/Ingest.tsx                OK 67 025 bytes compiled

Backend SSE shapes the page consumes (profile + approve) were
already verified end-to-end in Steps 2 and 3.

The interactive flow itself (drag-drop, timeline updating live,
proposal card edits, progress bars advancing, done summary,
navigation buttons) was NOT verified in this environment — no
headless browser available. The same caveat from entry 22
applies. The user should run `docker compose up frontend
backend`, open http://localhost:3000/ingest, drop an .xlsx
that ISN'T in the production checkpoint (so the embed path
exercises live), and confirm:

  - dropzone shows filename on drop, Analyse button enables;
  - clicking Analyse moves to Step 2 and the timeline grows
    event-by-event;
  - ProposalCard appears with column mapping table and
    editable client/date inputs;
  - clicking Approve moves to Step 3, all four progress bars
    advance, completes within ~30s for a small RFI;
  - done summary shows correct chunk counts;
  - Add another / Go to answer buttons reset/navigate.

**What it teaches.**

The Ingest page is ~430 lines of TSX but only ~120 lines are
the wizard's actual state and transitions; the rest is the
JSX tree (cards, the dropzone, the timeline list, the progress
list, the done summary). That ratio is what well-shaped
composition feels like: most of the visible code is the
*description* of what the screen looks like, not the
*orchestration* of which API calls happen when.

A naive implementation that mixed orchestration into the JSX
(inline async functions in onClick handlers, fetch inside
useEffect with cleanup, manual EventSource creation in
mounts) would be the same length but unreadable. The shape
that works here:

  1. State is named and lives at the top of the function.
  2. Async transitions live as named callbacks just below.
  3. JSX describes the screen and triggers the named
     callbacks; it doesn't construct fetch() calls or
     instantiate EventSources.
  4. Derived state goes through useMemo.

This is the "page is a controller, lib is the model" split.
Other UI frameworks make this explicit (MVC, MVVM); React
doesn't enforce it but rewards it. Future pages — Answer in
particular — will follow the same shape because the
underlying SSE-driven nature is the same shape.


## 24. UI Step 9 — Answer workflow (per-question SSE + review + export)

**SPEC_UI Step 9 deliverable.** The other interactive flow, and
the one the CPO singled out as load-bearing because of the
verbose provenance (LEARNING_NOTES entry 12). Upload a new client
RFI; the backend extracts questions; the SSE stream emits one
`answer` event per question with the full retrieval trace;
AnswerCards stack as they arrive with Accept / Edit / Skip
controls. When the stream completes, a Review section renders a
status table and the ExportButton.

**Decisions made in code, with the load-bearing reasoning.**

*Cards stack and become interactive as they arrive, not at the
end of the stream.* The SSE design (entries 17 + 19) was built
around streaming-not-batching specifically so a long RFI
doesn't keep the user waiting for the whole list before they
can act. The frontend honours that: the moment an `answer`
event lands, its card appears with full controls. The user can
Edit Q3 while Q15 is still generating in the background. This
is the difference between "watch a progress bar for 5 minutes"
and "review answers as they're produced", and it changes the
felt cost of the workflow by an order of magnitude.

*Per-card state is page-local, keyed by question index, NOT
sent to the backend until export time.* During the streaming
phase, every Accept / Skip / Edit is a local state mutation.
The backend doesn't learn about it until the user clicks the
Download button, at which point ExportButton POSTs the
collected overrides + skipped to `/api/answer/edit` and then
GETs `/api/answer/export`. Sending each click to the backend
in real time would add round-trips for state the user might
change again before exporting (Accept then Edit then Skip).
Buffering on the client is simpler and the backend's
single-write-on-disk-then-export shape matches it.

*`pending` is tracked explicitly even though the backend
treats `pending` and `accepted` identically.* On the wire,
neither pending nor accepted appears in the export body
(both fall through to "write the generated answer"). The
UI distinguishes them so the review table can show
"3 still pending review" — a user who streamed 28 answers
and reviewed 25 of them sees that 3 are unreviewed before
clicking download. The backend-equivalence stays clean; the
UI adds the bookkeeping that helps the human.

*Refused answers ("I cannot find this in our corpus.") get a
`no corpus match` badge but no automatic skip.* A refusal IS
the answer the system produced; the reviewer might choose to
let it land in the export ("we don't have anything for this,
the human will draft it"), accept it as-is, edit it into a
manual draft, or skip it. All four are valid. Auto-skipping
refusals would foreclose the "let the refusal land so we know
we owe a manual answer" pattern. The badge surfaces the state;
the user decides.

*The Source list is collapsible per source, NOT collapsed by
default at the source-list level.* Every retrieved chunk's
filename + row + score is visible on the card without
expansion (that's the verbose-provenance mandate). Clicking a
specific source row expands to show its question_text +
answer_text inline — the underlying past Q&A. Two levels of
visibility: "what's the trace" at glance, "what did the chunks
actually say" on demand. The opposite shape (whole sources
list collapsed behind "show details") would hide the trace by
default, defeating the purpose. The current shape is the
narrowest disclosure pattern that still lets the user verify
chunks without flooding the page.

*Cross-tenant warning renders BEFORE the action buttons, in a
high-contrast yellow panel.* When the answer mentions a past
client by name (`mentioned_clients` non-empty from the backend
word-boundary regex per entry 19), the warning is in the
card's flow above the buttons. The user cannot click Accept
without seeing it. Position matters here: a warning rendered
below the buttons, or as a tooltip on a Badge, would be
trivially missable. The visual weight (yellow border-left, the
AlertTriangle icon, plain English explanation) intentionally
breaks the card's visual rhythm.

*ExportButton uses `window.location.href` for the download,
not fetch + Blob + anchor.* The backend's FileResponse sets
Content-Disposition: attachment. In every modern browser,
navigating to a URL with that header triggers a download
without unloading the current page — the React app keeps
running. Fetch + Blob would re-stream the bytes through
JavaScript memory unnecessarily, which is wasteful for any
file size and would prevent the browser's built-in download
manager UI from appearing.

*The Review table appears UNDER the cards, not replacing them.*
When the done event arrives, the page doesn't transition to a
new "review" view; it appends a Review section to the existing
cards. The user can scroll up to re-edit a card after seeing
the summary. Replacing the cards with a table would make the
common case "I want to revise an edit I made earlier" require
navigating back through state.

**Verification.** Three TSX files compile via Vite without
errors:

  /src/components/AnswerCard.tsx     OK 47 856 bytes compiled
  /src/components/ExportButton.tsx   OK  9 115 bytes compiled
  /src/pages/Answer.tsx              OK 70 282 bytes compiled

Backend SSE shape consumed by this page (`/api/answer/process`)
was end-to-end verified in Step 4, including the
cross-tenant-leakage flags that this UI surfaces.

Interactive flow NOT verified — same headless-browser caveat
as Steps 7 and 8. Recommended manual check: upload a fresh
.xlsx via /answer, wait for several answers to arrive, try
Accept on one, Edit on another (verify textarea editable +
Save persists), Skip on a third. After done event arrives,
the Review section should show all status badges including
"cross-tenant" outline badges where appropriate, and the
download should produce an .xlsx with the three exporter
columns.

**What it teaches.**

The pattern that emerged across Steps 6-9 is consistent enough
to name:

  1. Page owns state + transitions (the controller).
  2. Lib owns typed wrappers + SSE hook (the model).
  3. Components are presentational — they take props and
     render JSX (the view).

Within the page, the order is also consistent:

  1. useState for primary state
  2. useSSE for streams
  3. useMemo for derived values from event arrays
  4. useEffect for one-shot side effects (mount clears,
     state seeding)
  5. Named callback handlers for transitions
  6. JSX tree that calls the named callbacks

When you follow this shape, the Ingest page and the Answer
page end up structured identically even though they do
different things. That's not by accident — it's because both
pages are SSE-driven wizards on top of a typed FastAPI
backend, and the shape FITS that problem. A reader who
understands one understands both immediately.

The contrast: had we written each page as ad-hoc fetches with
inline async/await in onClicks, the two pages would have
diverged into "two custom-grown things" and each one would
need to be read independently. Convention pays for itself the
second time it's followed.


## 25. UI Step 9.5 — Delete an RFI from the corpus

**Why this step exists.** Not in SPEC_UI's nine ordered steps,
but added after the user's "will we be able to delete a specific
fake RFI from the database post upload?" — a question that
exposed a real gap: a user testing the ingest workflow with a
throwaway file had no UI affordance to clean up afterwards.
Without delete, the only escape was `python -m pipeline.ingest
--reset` which nukes EVERYTHING. This step adds the precise
primitive: "delete one RFI".

**Decisions made in code, with the load-bearing reasoning.**

*Delete removes chunks + checkpoint + config, but KEEPS the
data/<filename>.xlsx upload on disk.* The user's intent is
"remove from corpus" — that's the chunks. Removing the
checkpoint entries prevents a future `python -m pipeline.ingest`
from re-adding what the user just deleted. Removing the
config_rfi_<slug>.json prevents the CLI from even SEEING the
RFI as ingestable (load_all_rows() globs configs). The data
file is small, harmless, and might be wanted again — keeping
it lets the user re-upload via the UI without losing the bytes.

An `?also_delete_file=1` query param could nuke the data file
too. Not added today because nobody is asking and the minimal
version is less destructive. The shape "delete is the user's
intent expressed in code" wins over "delete is whatever
clear-corpus means today".

*Extend GET /api/corpus/stats to return per-file chunk counts,
don't add a separate /list endpoint.* The Landing page now
needs richer per-file data (chunks count for display) but the
shape "give me corpus state" is one logical concept. Splitting
into /stats (totals) + /list (per-file) would mean two HTTP
calls on Landing for what is one user-facing question. The
upgrade from `files: string[]` to `files: {source_file,
chunks}[]` is a breaking change to the prior schema, BUT this
is a single-deployment private project — no external consumer
of the API exists to break. Clean cut.

*Delete validates source_file against path-traversal characters
("/", "\\") at the boundary.* The endpoint takes a free-text
query param and feeds it to file/glob operations
(config_rfi_<slug>.json deletion, ChromaDB metadata match).
Without the validation, a malicious source_file like
"../../etc/passwd" wouldn't actually escape (the slug
derivation strips non-alphanumerics so the resulting unlink
target would be path-safe), but defending at the boundary is
cheaper than auditing every downstream path. Two characters'
worth of checks, no performance cost, plus the explicit 400
error makes debugging clearer than a silent no-op.

*The confirm Dialog is mandatory; no "are you sure?"-bypassing
modifier-click shortcut.* Delete is destructive and irreversible
(the chunks would need to be re-embedded from the original Excel
to come back, costing a Mistral round trip per pair). A
"power user" shortcut that bypasses the confirm would optimise
for the wrong action — accidentally deleting a 140-pair RFI
because the click landed on the wrong row is much worse than
clicking through one extra dialog. The dialog text spells out
what stays vs what goes ("data/<x>.xlsx stays in case you want
to re-upload") so the user reads what they're committing to.

*The Landing page reloads stats by bumping a `reloadKey`
useState that drives a useEffect dep array, not by re-calling
getCorpusStats() directly from the delete handler.* The pattern
"increment a key to re-run an effect" is the React-idiomatic
way to express "refetch after this action". The alternative
(call getCorpusStats() inline) would mean the loading state
isn't shown during the refetch — the table would just freeze
on the old data until the new arrives. With the key-bump
pattern, the table goes to loading state cleanly because the
effect's start sets `stats` back to null first.

**Verification.** Tested against a real test artifact the user
left in the corpus during Step 8 verification
(`FakeGuardian_RFI_Duplicate_Testing.xlsx`):

  GET /api/corpus/stats  (before)
    -> total_pairs: 329, source_files: 5, files: [5 entries
                     including FakeGuardian...]

  DELETE /api/corpus/rfi?source_file=FakeGuardian_RFI...
    -> {chunks_removed: {
           rfi_combined_cosine: 50,
           rfi_combined_l2: 50,
           rfi_separated_cosine: 100,
           rfi_separated_l2: 100
        },
        total_chunks_removed: 300,
        checkpoint_entries_removed: 4,
        config_removed: true,
        config_path: "config_rfi_fakeguardian_rfi_duplicate_testing.json"}

  Chunk math checks out: 50 rows in the file × 1 chunk/row in
  combined collections = 50 chunks; × 2 chunks/row in separated
  (question + answer) = 100. Both confirmed.

  GET /api/corpus/stats  (after)
    -> total_pairs: 279, source_files: 4, files: [4 entries —
                     production corpus, FakeGuardian gone]

  Frontend Vite-compiles all four changed files (Landing.tsx,
  dialog.tsx, api.ts, plus the unchanged components) with no
  errors.

Browser interaction (clicking the trash button, seeing the
Dialog, confirming, watching the table refresh) was NOT
verified in this environment per the standing
headless-browser caveat. The user should sanity-check that
flow manually.

**What it teaches.**

The delete feature ended up at ~150 LOC across backend (90)
and frontend (60). Most of that is the Dialog confirmation
boilerplate; the actual deletion logic is a 20-line loop over
COLLECTIONS calling `coll.delete(where={"source_file": X})`.
The smallness is a direct consequence of two earlier decisions:

  1. Ingest stamps every chunk with `source_file` metadata
     (entry 9). Delete is just the inverse query.
  2. The disk format chain
     (config_rfi_*.json + outputs/.ingest_checkpoint.json) is
     stable enough that a delete operation knows exactly which
     files to touch.

Without those, delete would have been a hairier operation —
walking chunks by ID, reconstructing what to delete from
opaque pair_id parsing, etc. With them, it's a one-liner in
ChromaDB plus the obvious filesystem cleanup. The corpus
contract from entry 18 ("the disk format IS the interface")
keeps paying out on operations beyond the original ingest +
query flow.

Generalised: when designing an immutable-write-and-query
system, plan for the inverse operation too. The shape that
makes inserts fast and queries good is usually the shape that
makes targeted deletes possible. Add the metadata stamp at
insert time and the delete-by-stamp operation is free; skip
it and the delete operation becomes its own engineering
project.


## 26. Production deployment shape — dev vs prod compose, what changes and why

**Context.** After Step 9.5 the UI was feature-complete and the
user wanted to deploy to a separate server. The dev compose was
not production-shaped — bind-mounts everywhere, `uvicorn
--reload` (development hot reload), Vite dev server (a full
Node runtime continuously bundling in memory), no
`.dockerignore` so `docker build` would have slurped client
data into the image. None of those would have been fatal in
production but each was wasteful or unsafe.

This entry documents the dev/prod split that landed in
`docker-compose.prod.yml` + `frontend/Dockerfile.prod` +
`frontend/nginx.conf` + the new `.dockerignore` files. The
shape is intentionally conservative — minimum viable
production, no Kubernetes, no service mesh, no orchestration
beyond Docker Compose. The deployment topology assumed is "one
small server behind the org's existing SSO + reverse proxy",
which matches the SPEC_UI.md out-of-scope statement on auth.

**Decisions made in code, with the load-bearing reasoning.**

*Two compose files (dev + prod overlay), not one
environment-driven file.* `docker compose -f
docker-compose.yml -f docker-compose.prod.yml up -d` overlays
the prod file on top of the dev file. The dev compose stays
unchanged — running `docker compose up backend frontend` on a
laptop still hits the dev-friendly defaults (bind-mounts, HMR,
--reload). Production differences are visible in one file with
clear comments. The alternative — environment variables and
Compose profiles inside one compose — would have made the
behavioural diff implicit and harder to review.

*Production backend keeps the dev bind-mount (`.:/app`), only
the command changes.* An earlier draft of the prod compose
tried to be more "production-ideal": replace the dev
bind-mount with targeted bind-mounts for ONLY chroma_db, tmp,
and data. That broke a real write path. The UI ingest writes
`config_rfi_<slug>.json` at the project root (where
`pipeline.ingest.load_all_rows` globs); the delete endpoint
updates `outputs/.ingest_checkpoint.json`. Neither path was on
the named volumes — they'd have lived in the container's
writable layer and vanished on restart.

The honest fix was to accept that the production server holds
a cloned repo and bind-mount the whole directory the same way
dev does, with `--reload` dropped. The "self-contained image"
property is given up for the backend, but it was always going
to be given up anyway because the pipeline writes through to
several locations the user wants persisted. The trade is
explicit now — clone the repo on the prod server, `git pull`
to update, no special update workflow.

The frontend IS self-contained in production because the
static bundle is the only artefact it serves and that bundle
goes in the image at build time. Symmetry isn't required —
the layers have different persistence needs.

*Production frontend is multi-stage (node build + nginx
serve), not Vite dev server.* The Vite dev server is fine
correctness-wise — it can serve a production deployment — but
runs a full Node process continuously bundling in memory. For
a long-running server that's hundreds of megabytes of resident
RAM and a moving attack surface (Vite is a build tool, not
hardened for production traffic). nginx-alpine is ~30 MB,
process-1-friendly, well-known to ops. The multi-stage
Dockerfile.prod throws the entire Node toolchain away after
the bundle is built — final image is build-toolchain-free.

*nginx config disables `proxy_buffering` for /api.* nginx's
default behaviour is to buffer responses until end-of-stream
then forward in one chunk. Applied to the SSE streams from
the FastAPI backend, this would defeat the entire point of
SSE — the user would see all events arrive at the moment of
stream close instead of incrementally. `proxy_buffering off;
proxy_cache off;` keep the per-event delivery intact. We
already set `X-Accel-Buffering: no` at the FastAPI layer
(api/CLAUDE.md), but the proxy-side flag is the
load-bearing one in a deployment where nginx is the
last hop before the browser.

*`proxy_read_timeout 1h` on /api.* The default 60-second
nginx timeout would kill an in-flight Mistral ingest of a
large RFI mid-stream (28 questions × ~3-5s each = up to
two minutes; eval runs are longer). One hour is a
deliberately generous cap — long enough for any single
realistic workload, short enough that a truly stuck stream
eventually fails cleanly rather than holding the connection
forever.

*`.dockerignore` mirrors `.gitignore` plus extras.* The two
files protect different surfaces:

  - `.gitignore` keeps content out of git history (where it
    would be irrecoverable post-leak).
  - `.dockerignore` keeps content out of built images (where
    it would ride along to whatever registry / production
    server / sharing surface the image touches).

Both must cover the same privacy-critical paths (data/*.xlsx,
chroma_db/, outputs/, tmp/, .env), but `.dockerignore` ALSO
excludes things that ARE git-committed but don't belong in
the backend image (`frontend/`, `docs/`, `.git/` itself).

*Production data/ bind-mount is read-write, not read-only.*
The first draft of `docker-compose.prod.yml` marked
`./data:/app/data:ro` because the data files are "user-managed
source RFIs". This was wrong — the ingester COPIES the upload
from `tmp/{sid}/upload.xlsx` to `data/<original_filename>` as
part of the approve flow (entry 18). Forcing read-only would
have broken the UI ingest workflow on production. Removed in
the same commit that introduced it.

**Verification.**

  `docker compose -f docker-compose.yml -f
   docker-compose.prod.yml build` — builds both images cleanly,
   frontend final stage is nginx-alpine + the built bundle.

  `docker compose -f docker-compose.yml -f
   docker-compose.prod.yml up -d` — starts both services.

  Browser visits http://localhost:3000/, Landing page renders
  with corpus stats fetched via nginx's `/api/*` proxy. The
  full ingest / answer / export workflows worked end-to-end
  through the production stack (manual verification by the
  user, per the standing headless-browser caveat).

**What it teaches.**

The smallest viable production-different-from-dev surface is:

  1. Drop hot-reload (server is long-running, not iteratively
     edited).
  2. Replace dev-mode bundler with a real web server for the
     static frontend.
  3. Set restart policies.

Everything else (bind-mounts, named volumes, registry pushes,
orchestration) is a trade-off based on the deployment scale
and ops model. For a single-server internal tool with a
cloned repo and a reverse proxy in front, the simpler shape
WINS — fewer moving parts, fewer files to keep in sync, less
deployment-specific knowledge required of the maintainer.

The smartness budget at "I want to put this on a server"
should be spent on what actually breaks (the .dockerignore
preventing client data from being baked into images, the
nginx buffering off for SSE), not on architectural patterns
that don't pay back at this scale (k8s manifests, secret
managers, GitOps reconciliation). The dev compose + prod
overlay split is what fits a small-team internal tool; bigger
shapes are available when bigger problems show up.


