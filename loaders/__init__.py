"""loaders — format-specific document parsers.

Each loader converts one file format into the common `Paragraph` model
(models/paragraph.py). Adding a new format means adding one loader here;
nothing downstream changes.

    load_docx(path) -> list[Paragraph]   — Microsoft Word .docx
    load_pdf(path)  -> list[Paragraph]   — PDF via pdfplumber

A small dispatcher `load(path)` picks the right loader by file extension,
so analyse.py / chunker.py can stay format-agnostic. Adding a third format
later is one new module + one entry in `_LOADERS_BY_EXT`.
"""

from pathlib import Path

from loaders.docx_loader import load_docx
from loaders.pdf_loader import load_pdf

# Dispatch table — single source of truth for "what file types are supported".
_LOADERS_BY_EXT = {
    ".docx": load_docx,
    ".pdf":  load_pdf,
}


def load(path):
    """Pick the right loader by file extension and return list[Paragraph].

    Raises ValueError for any unsupported extension. The error names every
    supported format so a caller hitting it knows immediately what to do.
    """
    ext = Path(path).suffix.lower()
    loader = _LOADERS_BY_EXT.get(ext)
    if loader is None:
        raise ValueError(
            f"Unsupported file extension {ext!r}. Supported: "
            f"{', '.join(sorted(_LOADERS_BY_EXT))}.")
    return loader(path)


__all__ = ["load", "load_docx", "load_pdf"]
