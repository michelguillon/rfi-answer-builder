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

<!-- Next entry goes here -->
