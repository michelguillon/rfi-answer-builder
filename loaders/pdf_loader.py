"""
loaders/pdf_loader.py — PDF -> list[Paragraph]  [Phase 2 / spec Step 5]
======================================================================
The PDF counterpart of docx_loader. Public interface mirrors it exactly:

    load_pdf(path: str) -> list[Paragraph]

so chunker.py and analyse.py do not care which format produced the
paragraphs they receive — the whole point of the common model.

PDFs are structurally different from .docx. There is no paragraph style
system, no numPr element, no inherited formatting — every layer of
"structure" must be INFERRED from visual properties (font size, font name,
position). pdfplumber exposes per-character font metadata, so we rebuild a
paragraph stream as:

    characters -> words -> lines (grouped by y) -> paragraphs (grouped by gap)

For each resulting paragraph:
    text          : concatenation of the words (single-space joined)
    style_name    : None        — PDFs have no styles. Set explicitly so
                                  signal `style==<name>` simply never fires.
    rendered_size : modal char size in the paragraph (robust to outliers)
    is_bold       : majority of words use a "bold"/"black"/"heavy" font
    has_num_pr    : line starts with a bullet glyph or a "1." / "-" prefix
    in_table      : paragraph's bbox sits inside one of pdfplumber's
                    detected tables
    source_format : "pdf"
    date, override: "" / False — not extracted from PDFs

KNOWN LIMITATIONS:
- Scanned (image-only) PDFs have no extractable text. Detected and raised
  loudly — we never return an empty list silently. OCR is out of scope.
- Multi-column layouts confuse word-flow reconstruction. CVs are usually
  single column; we use pdfplumber's use_text_flow=True heuristic.
- Bold inference is by font-NAME ("Arial-BoldMT" etc.). PDFs that render
  bold via synthesised stroke width rather than a bold-named font will
  read as non-bold. Acceptable for the typical CV.
"""

import re

import pdfplumber

from models import Paragraph

# Bullet glyphs commonly encountered in PDFs. Numbered lists ("1.", "1)")
# and dash/asterisk lists are matched separately in the regex below.
BULLET_GLYPHS = "•●▪◦‣■□◆"
# NOTE: "·" (U+00B7 middle dot) is NOT in this set on purpose — many CVs use
# it as an inline separator (e.g. "skill A · skill B"), not as a bullet
# marker. Adding it would misclassify those lines as list items.

# The leftmost WORD of a line. pdfplumber emits a bullet glyph as its own
# word (separate from the bullet's text), so we test that word as a whole
# rather than scanning a prefix of the joined line text — fewer false
# positives, and we know the marker is positionally distinct.
_LIST_MARKER_RE = re.compile(
    r"^(?:["
    + re.escape(BULLET_GLYPHS)
    + r"]|\d+[\.\)]|-|\*)$"
)

# Tolerances for grouping. Pure heuristics — these values come from
# eyeballing the sample CV PDF and may need tuning per document class.
LINE_TOP_TOLERANCE = 2.5     # pt — words within this y-distance = same line
PARA_GAP_FACTOR    = 1.4     # gap > (line height * factor) -> new paragraph
SIZE_CHANGE_PT     = 1.0     # rendered-size delta that triggers a new para
TABLE_BBOX_PADDING = 2.0     # pt — slack when testing "inside a table" bbox

# Font names that indicate bold rendering. Not perfect (PDFs sometimes
# synthesise weight without naming the font Bold), but covers the common case.
_BOLD_TOKENS = ("bold", "black", "heavy", "semibold", "demi")


def _is_bold_font(fontname):
    """Heuristic: a fontname containing one of the bold tokens marks bold.

    Common PDF font naming: 'ArialMT', 'Arial-BoldMT', 'TimesNewRoman-Bold',
    'Helvetica-Black'. Matching is case-insensitive on the full name.
    """
    if not fontname:
        return False
    name = fontname.lower()
    return any(tok in name for tok in _BOLD_TOKENS)


def _group_words_into_lines(words):
    """Group word dicts into a list-of-lines by y-position.

    pdfplumber gives every word a bbox (x0, top, x1, bottom). Words whose
    `top` values are within LINE_TOP_TOLERANCE belong to the same line.
    Returns lines sorted top-to-bottom, words within each line sorted left-
    to-right.
    """
    words = sorted(words, key=lambda w: (w["top"], w["x0"]))
    lines = []
    current = []
    line_top = None
    for w in words:
        if line_top is None or abs(w["top"] - line_top) <= LINE_TOP_TOLERANCE:
            current.append(w)
            if line_top is None:
                line_top = w["top"]
        else:
            lines.append(current)
            current = [w]
            line_top = w["top"]
    if current:
        lines.append(current)
    for line in lines:
        line.sort(key=lambda w: w["x0"])
    return lines


def _line_height(line):
    """Max char height across the words of a line, in points."""
    return max((w.get("bottom", 0) - w.get("top", 0) for w in line), default=0)


def _line_signature(line):
    """Return (modal_size, dominant_fontname, bold_majority) for a line.

    Three signals that together describe what the line LOOKS like:
      - modal_size: most common rounded font size across all chars
      - dominant_fontname: the font name worn by the most chars
      - bold_majority: True if more than half the chars use a bold font

    char-weighted (one vote per character, not per word) — a single bullet
    glyph in a different font does not steal the line's signature from its
    50-character text payload. That is exactly what we want for `- bullet
    text` lines where the marker is Cambria but the text is Calibri.
    """
    if not line:
        return (None, None, False)
    fontnames = []
    sizes = []
    for w in line:
        fn = w.get("fontname", "") or ""
        sz = w.get("size")
        n = len(w.get("text", ""))
        if not n:
            continue
        fontnames.extend([fn] * n)
        if sz is not None:
            sizes.extend([round(float(sz), 1)] * n)
    bold_chars = sum(1 for fn in fontnames if _is_bold_font(fn))
    is_bold = len(fontnames) > 0 and bold_chars > len(fontnames) / 2
    counts = {}
    for fn in fontnames:
        counts[fn] = counts.get(fn, 0) + 1
    dom_font = max(counts.items(), key=lambda kv: kv[1])[0] if counts else None
    return (_modal_size(sizes), dom_font, is_bold)


def _line_starts_with_marker(line):
    """True if the leftmost word of the line is exactly a list marker."""
    if not line:
        return False
    return bool(_LIST_MARKER_RE.match(line[0].get("text", "")))


def _group_lines_into_paragraphs(lines):
    """Group consecutive lines into paragraphs.

    Four signals create a paragraph break, applied in order. Each captures
    a different real reason the visual block changes:

      1. The line starts with a list marker (bullet glyph, '-', '1.', etc.).
         This is the PDF analogue of <w:numPr>: each bullet item is its own
         paragraph, regardless of vertical distance from the previous line.
      2. The vertical gap is more than PARA_GAP_FACTOR * line height. A
         tighter threshold than v1 of this loader: two consecutive headings
         in the same font (e.g. "Company" then "Job Title" stacked) sit ~1.5x
         apart, while continuation lines of body text sit ~1.2x apart, so
         the boundary lies between them at ~1.4x.
      3. Bold state flips. "Core Skills" (BoldItalic) -> body (regular) is
         the textbook case.
      4. Modal font size shifts by SIZE_CHANGE_PT or more. Headings into
         body. The 1pt floor avoids breaking on rounding noise.
      5. Dominant font name changes (e.g. Cambria heading -> Calibri body).

    Critically, the FIRST signal (marker) is independent of the others, so
    a bullet line whose font and size happen to match the previous line
    still becomes its own paragraph — the correct behaviour for tight lists.
    """
    if not lines:
        return []
    paragraphs = [[lines[0]]]
    prev_sig = _line_signature(lines[0])
    prev_bottom = max(w.get("bottom", 0) for w in lines[0])

    for line in lines[1:]:
        sig = _line_signature(line)
        cur_top = min(w.get("top", 0) for w in line)
        gap = cur_top - prev_bottom
        line_h = _line_height(line) or 12.0

        size_changed = (prev_sig[0] is not None and sig[0] is not None
                        and abs(prev_sig[0] - sig[0]) >= SIZE_CHANGE_PT)
        font_changed = (prev_sig[1] and sig[1] and prev_sig[1] != sig[1])
        bold_changed = prev_sig[2] != sig[2]

        if (_line_starts_with_marker(line)
                or gap > line_h * PARA_GAP_FACTOR
                or bold_changed
                or size_changed
                or font_changed):
            paragraphs.append([line])
        else:
            paragraphs[-1].append(line)

        prev_sig = sig
        prev_bottom = max(w.get("bottom", 0) for w in line)

    return paragraphs


def _modal_size(sizes):
    """Most common size in a paragraph — robust to one-off outliers
    (superscripts, page numbers stitched in by accident)."""
    if not sizes:
        return None
    counts = {}
    for s in sizes:
        counts[s] = counts.get(s, 0) + 1
    # Tie-break by max — slightly prefer the larger size when counts are equal
    # so a heading line dominated by one-off characters still reports as large.
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _paragraph_from_lines(plines, in_table):
    """Build a Paragraph from a list-of-lines (each a list of word dicts).

    Returns None for an empty paragraph (whitespace-only).

    has_num_pr is True iff the FIRST line's leftmost word is a list marker
    (the same condition that caused this paragraph to be split off in
    `_group_lines_into_paragraphs`). The marker word stays in the text —
    downstream chunking does not care about leading punctuation, and
    stripping it would lose evidence that we recognised it.
    """
    words = [w for line in plines for w in line]
    if not words:
        return None
    text = " ".join(w["text"] for w in words).strip()
    if not text:
        return None
    sizes = [round(float(w.get("size")), 1) for w in words
             if w.get("size") is not None]
    bold_count = sum(1 for w in words if _is_bold_font(w.get("fontname")))
    is_bold = bold_count > len(words) / 2
    return Paragraph(
        text=text,
        style_name=None,                       # PDFs have no styles
        rendered_size=_modal_size(sizes),
        is_bold=is_bold,
        has_num_pr=_line_starts_with_marker(plines[0]),
        in_table=in_table,
        source_format="pdf",
    )


def _paragraph_in_table(p_words, table_bboxes):
    """True if the paragraph's bbox is (mostly) inside any detected table."""
    if not table_bboxes:
        return False
    px0 = min(w["x0"]     for w in p_words)
    py0 = min(w["top"]    for w in p_words)
    px1 = max(w["x1"]     for w in p_words)
    py1 = max(w["bottom"] for w in p_words)
    for tx0, ty0, tx1, ty1 in table_bboxes:
        if (px0 >= tx0 - TABLE_BBOX_PADDING
                and py0 >= ty0 - TABLE_BBOX_PADDING
                and px1 <= tx1 + TABLE_BBOX_PADDING
                and py1 <= ty1 + TABLE_BBOX_PADDING):
            return True
    return False


def load_pdf(path):
    """Flatten a .pdf into a list of Paragraph objects.

    Raises ValueError if the document yields no extractable text — almost
    always a scanned PDF, which needs OCR (out of scope here). Failing
    loudly here matches the Phase 2 design principle: silent empty output
    is the worst failure mode in a data pipeline.
    """
    paragraphs = []
    total_text_chars = 0

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(
                extra_attrs=["fontname", "size"],
                use_text_flow=True,
            )
            total_text_chars += sum(len(w.get("text", "")) for w in words)
            if not words:
                continue

            try:
                tables = page.find_tables()
                table_bboxes = [tuple(t.bbox) for t in tables]
            except Exception:
                table_bboxes = []

            lines = _group_words_into_lines(words)
            for plines in _group_lines_into_paragraphs(lines):
                p_words = [w for line in plines for w in line]
                in_table = _paragraph_in_table(p_words, table_bboxes)
                para = _paragraph_from_lines(plines, in_table)
                if para is not None:
                    paragraphs.append(para)

    if total_text_chars == 0:
        raise ValueError(
            f"No extractable text found in {path}. This is almost always "
            f"a scanned (image-based) PDF — pdfplumber needs a text layer. "
            f"OCR is out of scope for this loader; convert with an OCR tool "
            f"(e.g. tesseract / ocrmypdf) before retrying."
        )

    return paragraphs
