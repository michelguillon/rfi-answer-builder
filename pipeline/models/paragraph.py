"""
models/paragraph.py — the common paragraph model  [Phase 2 / spec Step 1]
=========================================================================
Every format-specific loader (docx_loader, pdf_loader) converts its native
output into a list of THESE objects before anything downstream touches it.
`chunker.py` and `analyse.py` only ever see `Paragraph` — they have no
knowledge of the source file format.

ARCHITECTURAL DECISION: a shared intermediate representation, not per-format
objects. `chunker.py` used to receive python-docx paragraph objects directly.
A PDF paragraph comes from a different library with a different object shape;
if `chunker.py` knew about both it would become a format-specific parser, which
defeats the separation of concerns. With this model, adding a new format is
adding one loader — nothing downstream changes. See docs/SPEC_PHASE2.md, Decision 2.

ARCHITECTURAL DECISION: the model carries `date` and `override` even though
they are not pure formatting attributes (text + style + size + flags).
  - `date` is content paired from a sibling table cell. The whole point of
    `docx_loader` is that role/education rows carry a date column; dropping it
    would regress chunk date-spans. PDFs have no date column → `date` stays "".
  - `override` (a direct run-level font-size override) is a diagnostic used by
    `analyse.py`'s consistency check C3. It is not user-facing.
Both default to empty/False so a loader that has no such concept (the PDF
loader) simply leaves them alone. This is the spec's Decision 2 model, plus
two pragmatic extras agreed in the Phase 2 architecture conversation.
"""

from dataclasses import dataclass


@dataclass
class Paragraph:
    """One paragraph of a document, format-agnostic.

    Fields:
      text          — the paragraph's plain text (stripped)
      style_name    — paragraph style ("Heading 1", "Normal", ...) or None.
                      PDFs have no style system, so the PDF loader sets None.
      rendered_size — font size in points AFTER the style-inheritance chain
                      and any run override is resolved, or None if unknown.
      is_bold       — True if the paragraph renders bold.
      has_num_pr    — True if it is a list item (a <w:numPr> element in docx;
                      an inferred bullet/number prefix in a PDF).
      in_table      — True if it came from inside a table.
      source_format — "docx" | "pdf". For diagnostics only — no code should
                      branch on this; that is what the common model prevents.
      date          — a date string paired from a sibling cell (docx table
                      date column). "" when the paragraph carries no date.
      override      — True if a direct run-level font-size override is present
                      (diagnostic for analyse.py's consistency report).
    """

    text: str
    style_name: str | None
    rendered_size: float | None
    is_bold: bool
    has_num_pr: bool
    in_table: bool
    source_format: str
    date: str = ""
    override: bool = False

    def __repr__(self) -> str:
        """Compact debugging view: style, size, and a text preview.

        The default dataclass repr dumps every field on one long line, which is
        unreadable when you print a list of 80 paragraphs. This shows the three
        things you actually scan for when debugging a parse.
        """
        size = "?" if self.rendered_size is None else f"{self.rendered_size:g}pt"
        style = self.style_name or "-"
        preview = self.text[:60] + ("…" if len(self.text) > 60 else "")
        return f"Paragraph(style={style!r}, size={size}, text={preview!r})"
