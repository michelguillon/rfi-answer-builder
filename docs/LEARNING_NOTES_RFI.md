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

<!-- Next entry goes here -->
