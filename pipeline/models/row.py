"""
models/row.py — the tabular row model  [spec Step 1]
=====================================================
The data model for a single Q&A row read from an Excel RFI. Every Excel
loader converts its native worksheet output into a list of THESE objects
before anything downstream touches it. `chunker`, `ingest`, and the
profiler only ever see `Row` — they have no knowledge of openpyxl cell
objects, sheet structure, or per-file column mappings.

ARCHITECTURAL DECISION: a *second* dataclass, separate from Paragraph.
We already have `Paragraph` for prose documents (docx, pdf). The
temptation is to reuse it — set `text = question + "\\n" + answer`,
leave the formatting fields (style_name, rendered_size, is_bold,
has_num_pr, in_table) as None, and pretend a Q&A row is just an unusual
paragraph. That would compile, but it would be semantically wrong:

  - `Paragraph` carries *formatting signals* — style, font size, bold,
    list status, table membership. Those signals exist because they
    discriminate headings from body text from list items in a flowing
    document. A spreadsheet row has none of that vocabulary: it has
    columns. A row is structurally tabular; a paragraph is structurally
    prose. Forcing one model to cover both means every downstream consumer
    has to know which fields are meaningful for which source format —
    exactly the leakage the common model was meant to prevent.

  - The chunker has a real branch in its future: prose documents get
    paragraph-aware chunking with overlap; Q&A rows get one (or two)
    chunks per row by construction. Pretending both flow through the
    same shape just hides the branch behind `if source_format == "excel"`
    checks scattered through downstream code.

The principle: a common intermediate model works when all formats share
the same structural vocabulary. When they don't, a second model is
cleaner than a forced abstraction. See `docs/rfi_LEARNING_NOTES.md`,
entry 1, for the rejected alternatives and the generalisable lesson.

ARCHITECTURAL DECISION: pair_id is part of the model, not derived.
The chunking-strategy experiment (Decision 3 in the spec) creates two
chunks per row in the "separated" strategy — one for the question, one
for the answer — and links them by `pair_id` so the answer can be
fetched after a question-side retrieval. A stable identifier is the
linkage. Deriving it ad hoc at chunk time would mean the loader and the
chunker have to agree on the same derivation rule independently;
attaching it to the row at load time means the rule lives in one place
and travels with the data.

ARCHITECTURAL DECISION: metadata is `dict`, not a fixed set of named
fields. The Excel profiler discovers structure rather than assuming it,
so the metadata keys are not known until profile time — one file may
expose `category`, another `region`, another nothing beyond `client` and
`date`. A dict accepts whatever the profiler extracted; the alternative
(adding nullable fields for every conceivable column role) bloats the
dataclass and still misses the next unforeseen role. The cost of `dict`
is that downstream code can't statically type-check metadata keys — that
cost is paid in exchange for not having to amend the model every time a
new client format shows up.

ARCHITECTURAL DECISION: source_format is a string, not an enum.
Same convention as `Paragraph`. The field is diagnostic only — no
downstream code should branch on it; routing by format is the loaders'
job. Keeping it a string avoids a one-value enum (only "excel" is
expected here) and keeps the two models visually consistent.
"""

from dataclasses import dataclass


@dataclass
class Row:
    """One Q&A row from an Excel RFI, format-agnostic to the worksheet.

    Fields:
      question      — the question text from the question column (stripped).
      answer        — the answer text from the answer column (stripped).
      context       — optional supporting context from a third column, or
                      None if the source file has no context column.
      metadata      — dict of additional column values captured at profile
                      time (e.g. category, client, date, region). Schema
                      is per-file; keys come from the approved config.
      source_format — "excel". Diagnostic only — downstream code routes
                      by loader choice, not by this field.
      source_file   — the source filename (basename, not full path) so
                      retrieval results can attribute back to a document.
      pair_id       — stable identifier for this row, used to link a
                      question chunk to its paired answer chunk in the
                      "separated" chunking strategy. Format set by the
                      Excel loader (e.g. f"{filename_stem}_row_{index}").
    """

    question: str
    answer: str
    context: str | None
    metadata: dict
    source_format: str
    source_file: str
    pair_id: str

    def __repr__(self) -> str:
        """Compact debugging view: source, pair_id, and a question preview.

        The default dataclass repr prints every field on one line —
        unreadable when scanning a list of 80 rows in the chunk reviewer.
        This shows the three things you actually look at when sanity-
        checking a load: which file the row came from, its stable id,
        and the first slice of the question text.
        """
        preview = self.question[:60] + ("…" if len(self.question) > 60 else "")
        return f"Row(source={self.source_file!r}, pair_id={self.pair_id!r}, question={preview!r})"
