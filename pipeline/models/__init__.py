"""models — shared data models for the RFI pipeline.

Two models live here, deliberately separate:

  - `Paragraph` — prose documents (docx, pdf). Carries formatting
    signals (style, size, bold, list status, table membership) used by
    the chunker to discriminate headings from body text from list items.

  - `Row` — Q&A rows from Excel RFIs. Carries question/answer/context
    plus a per-file metadata dict. The structural vocabulary is "columns",
    not "formatting", so it is a separate model rather than a contorted
    Paragraph. See models/row.py and docs/rfi_LEARNING_NOTES.md, entry 1.

Downstream code writes `from models import Paragraph, Row` and does not
reach into the submodules.
"""

from pipeline.models.paragraph import Paragraph
from pipeline.models.row import Row

__all__ = ["Paragraph", "Row"]
