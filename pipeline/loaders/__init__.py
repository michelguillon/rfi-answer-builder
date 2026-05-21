"""loaders — format-specific document parsers.

Two model targets live in this package:

  Prose loaders → list[Paragraph]
    load_docx(path) -> list[Paragraph]   — Microsoft Word .docx
    load_pdf(path)  -> list[Paragraph]   — PDF via pdfplumber
    Common entry point: load(path)       — extension dispatch

  Tabular loader → list[Row]
    load_excel(path, config) -> list[Row]  — Excel via openpyxl

The prose loaders share a single-argument shape and a common output
model (`Paragraph`), so `load(path)` dispatches by extension. The
Excel loader takes a second argument (the human-approved column→role
config produced by profile_excel.py) and returns the different `Row`
model, so it is exported on its own rather than smuggled into the
prose dispatcher.

ARCHITECTURAL DECISION: do not unify Excel into the path-only dispatch.
A unified `load(path)` would either need a hidden config-loading step
(which couples the dispatcher to the on-disk config layout) or accept
an optional config arg that only one loader uses (which lies about
the contract for the others). The honest design is two entry points
for two contracts; the dispatcher exists for the case it was designed
for (extension → loader, single input).
"""

from pathlib import Path

from pipeline.loaders.docx_loader import load_docx
from pipeline.loaders.excel_loader import load_excel
from pipeline.loaders.pdf_loader import load_pdf

# Dispatch table — for prose loaders only. Excel is excluded because
# it has a different signature (see the load_excel export below).
_LOADERS_BY_EXT = {
    ".docx": load_docx,
    ".pdf":  load_pdf,
}


def load(path):
    """Pick the right loader by file extension and return list[Paragraph].

    Prose formats only. For Excel, call load_excel(path, config)
    directly — the Excel loader needs a config dict and cannot be
    selected by extension alone.

    Raises ValueError for any unsupported extension. The error names every
    supported format so a caller hitting it knows immediately what to do.
    """
    ext = Path(path).suffix.lower()
    loader = _LOADERS_BY_EXT.get(ext)
    if loader is None:
        raise ValueError(
            f"Unsupported file extension {ext!r}. Supported (prose): "
            f"{', '.join(sorted(_LOADERS_BY_EXT))}. For Excel, use "
            "load_excel(path, config) directly.")
    return loader(path)


__all__ = ["load", "load_docx", "load_pdf", "load_excel"]
