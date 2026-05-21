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


