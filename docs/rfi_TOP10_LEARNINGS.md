# RFI Answer Builder — Top 10 Learnings
## For interview preparation and portfolio conversations

These are the ten most transferable insights from building the RFI Answer Builder
— pipeline, eval, and production UI combined. Each one is stated as a claim you
can defend with a specific example from the build.

---

### 1. The hard problem wasn't retrieval. It was data quality.

When input shape is controlled by someone else, assumption is a bug waiting to happen.
The right primitive is discover + persist + verify, not "encode the expected shape in
the code."

**The example:** Three real RFI Excel files had nothing in common — different column
letters, different header rows, one file with a 11-row metadata preamble before the
actual Q&A, one file with six sheets where the RFI was the second-largest, not the
largest. v1 of the profiler broke on three of four files within twenty minutes of
running on real data. The retrieval eval that followed was largely anticlimactic;
the discovery architecture was where the real engineering happened.

**The generalisation:** In any production RAG system over externally-sourced documents,
schema discovery is a first-class component. The corpus quality determines the ceiling
on retrieval quality; no amount of retrieval tuning compensates for ingesting the
wrong columns.

---

### 2. Stack layers with complementary failure modes, not the same ones.

A three-layer detection pipeline (heuristic → LLM → human validator) is more robust
than a single sophisticated layer because the layers fail on disjoint cases. The value
is in the disjointness, not the number of layers.

**The example:** The heuristic catches deterministic easy cases (header label "Question")
cheaply. The LLM catches phrasing-variation cases ("Item", "What would you like to
know?", schema inference from sample data). The validator catches LLM constraint
violations (non-determinism, invented role names, the same column assigned two roles)
before the human sees the proposal. The human catches semantic mistakes — "this is
actually a form, not a Q&A table" — that no machine layer has the context to see.

**The interview framing:** "I didn't trust any single layer to be robust on real-world
inputs I didn't control. I designed each layer to catch what the layer below it misses.
That's why the validator runs between the LLM and the human rather than after the human
approves — by then, the human has already said yes to a broken mapping."

---

### 3. Validate before showing humans. Reserve human attention for semantic correctness.

Approval gates degrade when asked to do work machines can do. A human who rejects a
mechanically-broken proposal will start rubber-stamping the next one. Pre-validate;
then show only proposals that pass mechanical checks.

**The example:** The profiler's validator runs after the LLM produces its mapping and
before the human sees it. It enforces: exactly one `question` column, exactly one
`answer` column, all roles in the allowed set, every column in the sheet mapped.
If the LLM violates any of these, the validator rejects the proposal and asks for
a re-run — the human never sees it. The human's job is to confirm that column B
really is the question and column C really is the answer, not to notice that the
LLM accidentally assigned the same role to two columns.

**The design principle:** The HITL gate is load-bearing for semantic correctness. Make
it load-bearing for that and nothing else. Every deterministic check that could fire
post-approval will eventually confuse a tired reviewer.

---

### 4. Retrieval ideology is wrong. Measure your corpus.

"Hybrid retrieval beats semantic" is a hypothesis, not a fact. It holds when queries
are terminology-specific and documents are sparse. It doesn't hold when the corpus is
small, paraphrase-rich, and queries are natural language.

**The example:** The spec recommended separated chunks + hybrid retrieval + cross-encoder
reranking + cosine distance as the production configuration. The 36-configuration eval
overturned three of those four: semantic outperformed hybrid on this corpus, L2
narrowly outperformed cosine, and LLM reranking edged cross-encoder on retrieval gap
rate. The system shipped the empirically-validated configuration, not the spec's
prediction.

**The interview framing:** "I ran a 36-configuration eval against a 20-question
ground-truth dataset before committing to a retrieval architecture. Three of my four
spec predictions were wrong. The eval was doing real work — it wasn't confirming priors."

---

### 5. Hallucination refusal and retrieval gap are opposite things. Conflating them makes a failure look correct.

Both produce the same output: "I cannot find this in our corpus." They mean opposite
things. Reporting them as a single refusal rate makes a retrieval failure look like
correct grounding behaviour.

**The example:** The eval framework tracks two separate metrics. `hallucination_refusal_rate`
measures questions that are genuinely out of scope — the system correctly refused.
`retrieval_gap_rate` measures in-scope questions that were refused anyway — the answer
IS in the corpus but the retrieval didn't surface it. The minimum retrieval gap rate
across all 36 configurations was 0.176: 3 of 17 in-scope questions refused by the
best-performing config. That floor is almost certainly explained by 14 "asked but
unanswered" source rows — a corpus property, not a retrieval bug.

**The generalisation:** Every RAG system has two failure modes. Name them differently,
measure them differently, and improve them differently.

---

### 6. LLM-as-judge has a calibration problem. Scores at ceiling aren't evidence of quality.

When faithfulness = 5.00 and relevance = 5.00 across all 36 configurations, the judge
is not discriminating — it's reflecting training priors about what answers to
faithful-sounding prompts look like. The discriminating signals were elsewhere.

**The example:** The LLM judge was scoring correctly within its defined scope: the
answers were genuinely faithful to the retrieved context and genuinely relevant to the
questions. The ceiling scores meant those dimensions weren't the differentiating factor
across configurations. The signals that actually ranked configurations were
`retrieval_gap_rate` (how often the system refused in-scope questions) and
`completeness` (scored with explicit anchors). The faithfulness and relevance scores
were useful as "operating correctly" checks, not ranking signals.

**The design fix:** Anchor the judge with explicit counter-examples ("a 5 means
publication-ready; a 3 means partially answers the question; a 1 means evasive or
wrong"). Or use paired comparison — "which of these two answers is better?" — where the
judge must differentiate rather than score independently.

---

### 7. Cross-tenant content leakage is a safety concern that quality metrics can't catch.

Faithfulness, relevance, and completeness measure the answer against the retrieved
context. They cannot catch a problem that is embedded in the retrieved context itself.

**The example:** A generated answer scored faithfulness=5.0 while containing the sentence
"the agreement between Utiq and Reach addresses the engagement of processors" — where
"Reach" is a past client whose name appeared in the retrieved Q&A pair. The answer was
faithful. It was also a confidentiality risk: surfacing one client's name in a response
drafted for a different client is, depending on context, a confidentiality concern, a
professionalism concern, and at minimum an awkward mistake.

The LLM judge had no way to flag this — faithfulness to context is exactly what the
judge was measuring. Human review caught it. The system was updated to scan generated
answers for known client names and flag them on every answer card. The pipeline-layer
fix (prompt guard + post-generation redaction) is documented for future implementation.

**The generalisation:** For any RAG system over a private multi-tenant corpus, cross-tenant
content leakage needs its own design, its own eval dimension, and its own production
control. It will not emerge from faithfulness or relevance scoring.

---

### 8. When wrapping a CLI as a UI, participate in the CLI's persistence model. Don't replace it.

The natural instinct when adding a web UI is to add a database. The better question is:
what does the CLI already persist, and how does the UI participate in that contract?

**The example:** The FastAPI backend writes the same `config_rfi_<slug>.json` the CLI
writes. Both write uploaded files to `data/`. Both update `.ingest_checkpoint.json`.
A session started in the UI and inspected from the CLI works because they share the same
artefacts. Adding a third entry point (a CI job, an API client) follows the same
pattern with zero changes to either existing entry point.

**The architectural principle:** An API wrapping a CLI is not a new system. It's a new
interface to an existing persistence contract. Design the contract first; derive the
interfaces from it. The UI that participates in the CLI's contract costs almost nothing
to add. The UI that replaces it creates migration debt the first time a new interface
appears.

---

### 9. Deterministic where the content is a fact; AI judgment only where it adds value.

The profiler's schema discovery has one LLM call. Everything surrounding it is
deterministic: heuristics, validation rules, human approval. The retrieval layer was
evaluated empirically and then fixed at deploy time. The generation layer is one LLM
call, surrounded by: a hallucination guard, a cross-tenant name scan, a `refused` flag,
a human review gate.

**The example:** Question column detection on files with a labelled header (e.g. column B
is labelled "Question") is done by a label-match heuristic, not by an LLM. The heuristic
is lossless on labelled files and fires before the LLM call. The LLM runs only when
the heuristic returns no clear winner. This is the same principle as cv-tailor's
role-line extraction: if the correct output is determinable before the model call,
don't ask the model to produce it.

**The interview framing:** "I used AI exactly where a deterministic rule would fail and
nowhere else. That's what keeps the system auditable — every AI call has a clear scope,
and everything outside that scope has a correct answer the system produces without
spending inference tokens."

---

### 10. The right sequence is: problem → system → AI. Not the reverse.

Every design decision in this system — the three-layer profiler, the 36-configuration
eval, the cross-tenant safety flag, the SSE streaming UI, the HITL answer review — has
a reason grounded in the actual workflow it was automating. None of them were chosen to
demonstrate a technique.

**The interview version:** "I started with a solutions team spending hours manually
reviewing past RFIs every time a new one arrived. I asked what a system that solved
that problem would need to do. The profiler exists because clients send inconsistent
Excel files. The eval exists because the spec's retrieval hypothesis was a prediction
that needed testing. The cross-tenant flag exists because a generated answer named a
past client in front of a different client during manual review. The system is what the
problem required, not what the technique suggested."

**The portfolio statement:** This is not a RAG demo. It's a document intelligence
system that happens to use retrieval-augmented generation as one of its components.
The retrieval architecture is the product. The draft answer is the output.

---

## Quick reference — decision codes

| Learning | Learning notes entries |
|---|---|
| 1. Data quality first | 2, 5, 6 |
| 2. Complementary failure modes | 3 |
| 3. Validate before showing humans | 4 |
| 4. Measure retrieval vs ideology | 11, 13 |
| 5. Hallucination vs retrieval gap | 10, 11 |
| 6. LLM-as-judge calibration | 11 |
| 7. Cross-tenant leakage | 14, 19 |
| 8. UI participates in CLI model | 15, 18 |
| 9. Deterministic vs AI judgment | 3, 10, 19 |
| 10. Problem → system → AI | Retrospective coda |
