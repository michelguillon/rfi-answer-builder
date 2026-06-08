# RFI Answer Builder — Project Retrospective

**Project duration:** ~8 weeks (pipeline + UI, Months 1–2 of the AI learning track)
**Lines of code:** ~3,500 Python + ~1,800 TypeScript/React
**Corpus size:** 1,646 chunks across 4 RFI documents (279 combined, 544 separated per distance metric)
**Eval configurations compared:** 36 (chunk strategy × retrieval × reranker × distance metric)
**Production status:** Live on home server behind Cloudflare Tunnel

---

## The framing that matters before anything else

This project did not start with RAG and look for a use case.

It started with a real operational problem — a solutions team answering hundreds of RFI
questions manually, reviewing the same historical documents every time a new client
submitted a questionnaire, with no systematic way to find the best past answer across
three years of institutional knowledge in inconsistent Excel files.

The sequence was:

```
Problem
  ↓
What kind of system would solve it?
  ↓
Which parts of that system benefit from AI?
  ↓
Which AI patterns apply?
```

Not the reverse. That ordering is what kept every design decision grounded. When the
eval overturned the spec's retrieval hypothesis (semantic beat hybrid on this corpus),
the answer was to follow the data — because the system was designed to measure, not to
demonstrate a technique.

This is worth stating because most RAG portfolio projects go the other direction: pick
vector databases and embedding models, then find a corpus to search. The difference
shows when real data arrives in an unexpected format and the assumptions break down.

---

## 1. What I thought I was building

A search-and-draft tool for RFI responses.

The mental model going in: ingest past RFIs into a vector database. When a new question
arrives, retrieve the most semantically similar past answers. Hand them to an LLM with
"draft a response from these sources." Cite the sources. Done.

The early spec reflected this. Excel files go in. Questions come out. RAG in the middle.
Clean, linear, four steps from problem to answer.

The implicit assumption: the hard problem was the retrieval quality — semantic vs hybrid,
chunk size, reranking. Get that right and the system would work.

The even more implicit assumption: the input data would be in a workable shape.

---

## 2. What I actually built

A multi-layer document intelligence system with adaptive schema discovery, empirically-selected
retrieval, cross-tenant safety controls, and a production web interface with human review
gates at every decision point.

The shift from "search-and-draft tool" to that description is not rebranding. It describes
a different thing:

**Schema discovery** — Three real RFI files had effectively nothing in common: different column
positions, different header rows, different sheet layouts, one file that was a form rather than
a table. The system had to discover structure rather than assume it. The profiler became
a three-layer pipeline — heuristic → LLM → validator → human — because no single layer
was robust to the full range of file shapes. A "question mark density" heuristic picks
the right sheet from a six-sheet workbook. Label-match picks the header row before a
question-mark fallback that would regress on prose-style questions. Each layer was motivated
by a real file that broke the layer below it.

**Empirical retrieval selection** — The spec's production recommendation was separated chunks +
hybrid retrieval + cross-encoder reranking + cosine distance. The 36-configuration eval
overturned three of those four. Semantic outperformed hybrid on a small paraphrase-rich
corpus. L2 narrowly outperformed cosine. LLM reranking edged cross-encoder on retrieval
gap rate and completeness. The system shipped the configuration the data recommended, not
the one the spec predicted. That distinction is the eval framework's entire purpose.

**Safety layer** — Generated answers copied past client names verbatim from retrieved chunks.
"Reach" appeared in an answer drafted for a different client because the underlying Q&A pair
said "the agreement between Utiq and Reach." LLM-as-judge scored that answer faithfulness=5.0,
because it was faithful to the context. The leakage wasn't in any metric dimension the
eval was measuring. Human review caught it. The system was updated to flag cross-client
name mentions on every answer card before export.

**Production UI** — The CLI pipeline became a web application where non-technical staff
could run both workflows end-to-end: upload a past RFI for ingestion, or upload a new
client RFI and receive per-question draft answers with full source provenance, editable
inline, exportable as a filled Excel file. Every slow operation streams via SSE so the
interface responds in real time rather than hanging while Mistral works through 47 questions.

**Dual-mode operation** — The UI doesn't replace the CLI's persistence model; it participates
in it. The same `config_rfi_<slug>.json` that the CLI writes, the UI reads. The same
ChromaDB collections that the CLI queries, the web answerer queries. Adding the UI
required zero changes to the pipeline layer.

The combination is what makes it a decision-support system. The models don't produce
the final answer. They produce a draft with explicit provenance that a human can accept,
edit, or skip. The audit trail — which sources were retrieved, what their scores were,
whether a past client was named — travels with every answer.

---

## 3. Biggest learning shifts

Each one is a before and after. The "before" is what I believed going in.
The "after" is what the build proved.

---

**1.**
> We believed: the hard problem is retrieval quality — get the embedding and ranking right
> and the system will work.
>
> We learned: the hard problem is data quality. Retrieval quality is irrelevant if the
> input shape is assumed rather than discovered.

Three real RFI files broke every assumption about column positions, header rows, and
sheet layout within twenty minutes of running v1 of the profiler on real data. The
profiler had to be rebuilt from heuristic-only to three-layer (heuristic → LLM →
validator) before a single chunk could be embedded. The eval framework that followed
was anti-climactic by comparison — the retrieval architecture worked; the discovery
architecture was the hard part.

The lesson transfers: in any production RAG system over externally-sourced documents,
schema discovery is a first-class component, not a preprocessing step you do once.

---

**2.**
> We believed: a single LLM call to infer column roles would be sufficient for
> schema discovery.
>
> We learned: stack layers with complementary failure modes. The trick is that each
> layer must catch different bugs.

Heuristic catches deterministic easy cases cheaply. LLM catches phrasing-variation
cases the heuristic can't. Validator catches LLM constraint violations (non-determinism,
invented roles, duplicate assignments) before the human sees the proposal. Human catches
semantic mistakes — context about what this RFI actually is — that no machine layer can see.

On the first real run, the LLM produced zero `question` columns for a form-style file
that genuinely has no question column. The validator caught it before the human ever
saw the broken proposal. That's the validator earning its keep on its first input.

---

**3.**
> We believed: the human approval gate should catch everything wrong.
>
> We learned: show humans only proposals that pass mechanical checks. Reserve human
> attention for semantic correctness — the judgement no machine can make.

Approval gates degrade when they're asked to do work machines can do. A human who has
rejected a "valid"-looking proposal once because a machine check fired post-approval
will start rubber-stamping the next one. Pre-validate, then show.

---

**4.**
> We believed: hybrid retrieval (semantic + BM25) would outperform pure semantic retrieval.
>
> We learned: corpus characteristics matter more than retrieval ideology.

The RFI corpus is small (~280–540 chunks) and paraphrase-rich (the same privacy and
security concepts described multiple ways across four RFIs). On this corpus, semantic
retrieval outperformed hybrid consistently. BM25's advantage on exact-term matching
didn't materialise because the queries are also natural language, not acronym-heavy
technical lookups. If the corpus were ten times larger with more terminology-specific
questions, the finding might reverse.

Measure your corpus before committing to a retrieval architecture. The answer is
empirical, not theoretical.

---

**5.**
> We believed: LLM-as-judge is a reliable evaluation tool.
>
> We learned: LLM-as-judge has a calibration problem. Faithfulness = 5.00 and
> Relevance = 5.00 across all 36 configurations is not evidence of quality — it is
> evidence the judge cannot discriminate.

The judge was measuring correctly within its scope. The scores were consistently at
ceiling because the answers were genuinely faithful and relevant to the retrieved context.
The discriminating signals were retrieval_gap_rate and completeness — the metrics that
measure whether the system answered, not just whether it answered faithfully.

For future eval design: anchor the judge with explicit counter-examples ("a 5 means X,
a 3 means Y, a 1 means Z"), or use paired comparison where the judge picks one of two
answers rather than scoring both on independent scales.

---

**6.**
> We believed: "refusal" means the system is working — it didn't hallucinate.
>
> We learned: hallucination refusal and retrieval gap are opposite things that must
> be tracked separately or one masks the other.

A hallucination refusal is the system working correctly: the question isn't in the
corpus, and the system said so. A retrieval gap refusal is the system failing: the
question IS in the corpus, but the retrieval didn't surface it. Both produce the same
output ("I cannot find this in our corpus."). Reporting them as a single "refusal rate"
makes a retrieval failure look like correct grounding behaviour.

The eval framework reports them separately. That distinction is not optional.

---

**7.**
> We believed: faithfulness and relevance scoring would catch answer quality problems.
>
> We learned: quality metrics are scoped to their definitions. Cross-tenant content
> leakage is not in any of those dimensions.

The generated answer scored faithfulness=5.0 for including "the agreement between Utiq
and Reach addresses the engagement of processors." It was faithful — the claim came
directly from a retrieved chunk. But "Reach" is a past client whose name should not
appear in answers drafted for a different client. The LLM judge correctly optimised
for faithfulness. It had no mechanism to notice the confidentiality concern.

Hand-verification by a domain expert caught it because the reviewer reads with an
implicit "would I be embarrassed to send this?" check. That check needs to be
explicit in production: a generation-prompt constraint, a post-generation name
redaction pass, and an eval metric scoring cross-client name occurrence.

---

**8.**
> We believed: metadata is useful for filtering.
>
> We learned: in multi-document RAG, metadata is load-bearing, not optional polish.

With four RFIs in one corpus from different clients at different dates, the difference
between "search all past answers" and "search only security-category answers" or "search
only 2023 answers" is a `where=` clause in ChromaDB. That `where=` clause only works
if category, client, and date are captured at profile time and stored with every chunk.

The metadata design at ingestion determines what queries are even possible at runtime.
In a multi-tenant production system, this is also the access control layer: tenant A
must never retrieve tenant B's chunks. The pattern built here is the same pattern.
Document it explicitly.

---

**9.**
> We believed: wrapping a CLI as a web UI means replacing the CLI's persistence model
> with a database.
>
> We learned: the UI should participate in the CLI's persistence model, not replace it.

The FastAPI backend writes the same `config_rfi_<slug>.json` the CLI writes. Both
write the uploaded Excel to `data/`. Both update the same `.ingest_checkpoint.json`.
A session that was started in the UI and resumed in the CLI works because they share
the same artefacts. Adding a third entry point (a CI job, an API endpoint) would
follow the same pattern with zero changes to either the CLI or the UI.

When wrapping a CLI as a UI, the question is not "how do I add a database?" It is "what
does the CLI already persist, and how does the UI participate in that contract?"

---

**10.**
> We believed: the goal was to understand RAG well enough to build a useful tool.
>
> We learned: the goal is to understand systems well enough to know where AI adds
> value — and where it doesn't.

The profiler's three-layer stack contains exactly one AI call (the LLM schema mapping).
Everything else is deterministic: heuristics, validation rules, human approval. The
retrieval layer has three modes, but the decision of which to use was made empirically
before shipping, not left as a runtime variable. The generation layer has one model call,
but it's surrounded by: a hallucination guard, a cross-tenant name scan, a `refused`
flag, a human review card with Accept/Edit/Skip.

At no point is the AI doing anything unsupervised. At every point, the AI is doing
exactly the thing a deterministic system couldn't do: inferring column semantics from
sample data, matching semantically similar questions across paraphrases, drafting
natural-language responses from retrieved evidence.

The right question when building with AI is not "where can I use AI?" It is "what
does this stage of the system need to produce, and is that something that benefits from
AI judgment, or does it have a correct answer that a deterministic rule can produce?"
That question has a different answer at each stage. Answering it per-stage is what
makes the system composable and defensible.

---

## 4. What I'd do differently

---

**Build the eval framework before comparing retrieval approaches.**

The eval was built after ingestion was working and hybrid retrieval was already
implemented. That sequencing meant the first round of manual testing (entry 12 —
hand-verifying CPO's questions) was the only quality gate before the eval ran. The
cross-tenant leakage issue (entry 14) was caught by hand-verification, not by eval.

A better sequence: define the eval dataset and scoring dimensions before building
retrieval mode 2 and 3. Every retrieval configuration decision is then validated
against the eval rather than against intuition. The spec's hybrid-first recommendation
would have been overturned in the first eval run rather than after manual testing.

---

**Design cross-tenant safety from the start, not as a deferred fix.**

The client name leakage problem (entry 14) was discovered during hand-verification.
The UI mitigates it by flagging known client names on every answer card. The pipeline-layer
fix (prompt guard + post-generation redaction) is still deferred.

For any RAG system over a private multi-tenant corpus, the cross-tenant content boundary
should be a first-class design constraint from the first day — not discovered when a
past client's name appears in an answer for a different client during a demo.

---

**Instrument schema discovery to build a corpus of file shapes.**

The profiler now handles four real file shapes well. File 5 will be different. The
escape-hatch CLI flags (`--sheet`, `--header-row`) exist for the cases the heuristics
miss, but using them requires knowing they missed.

A better design: log every auto-detect decision to a structured audit file, including
what fired and what was considered. That creates a growing evidence base for profiler
improvement: when file 5 breaks the heuristic, the log shows exactly why, and the fix
can be tested against all five files rather than derived from first principles again.

---

**Measure retrieval gap rate against corpus properties earlier.**

The minimum retrieval_gap_rate across all 36 configurations was 0.176 — 3 of 17
in-scope questions were refused even by the best-performing configuration. Those three
refusals almost certainly correspond to the 14 "asked but unanswered" rows in the source
corpus (rows where the question is present but the answer cell is empty). If that
hypothesis is correct, the floor is a corpus property, not a system bug — and improving
it means getting better source data, not tuning retrieval parameters.

That hypothesis should have been verified before the 36-configuration eval ran. It would
have changed the framing from "how do we reduce retrieval gaps?" to "we have the best
retrieval possible given this corpus; the gaps are upstream."

---

## Coda

The most durable frame for this project, in retrospect, is not "Month 2 of an AI learning
track." It is a proof of concept for a production pattern that appears in any serious
document intelligence deployment:

- Input shape is controlled by someone else; the system discovers rather than assumes
- Multiple validation layers with complementary failure modes, not one smart layer
- Quality measurement that distinguishes system failures from corpus properties
- Cross-tenant content boundaries as a first-class safety constraint, not an afterthought
- Human review at every consequential decision point, not just at the end
- CLI and UI as two interfaces to the same persistence contract, not two systems

Each of those is a general principle. This RFI system is one instantiation of them for
one workflow. The next instantiation will look different, but the principles transfer.

The shift from "RAG project" to "document intelligence system" is what makes that
transfer possible.
