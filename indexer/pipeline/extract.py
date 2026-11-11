"""Stage 1: Extract. PDF, HTML, Markdown or text to page-by-page light Markdown.

The first stage loses the most information (failure mode A), so it is built around
what the real corpus does, measured on the FAST-NUCES files (docs/PIPELINE.md):

- Multi-column layout. The 2026 handbooks print two pages side by side on one PDF
  page and the prospectus uses four columns. ``page.extract_text()`` interleaves the
  columns line by line, gluing unrelated clauses together. We find the empty vertical
  gutters from word positions and read each column top to bottom. Lines that cross a
  gutter (page headers, full-width headings) are read as their own band, in order.
- Tables. pdfplumber's ruled-table finder also reports page frames and header boxes;
  we keep only real tables (not page-sized, not in the header/footer band, at least
  2x2 with half the cells filled) and render them as ``| a | b |`` rows, so a value
  stays next to its column header. Table text is removed from the prose.
- Headings. In these PDFs headings are set larger than body text (12 pt vs 10 pt), so
  a short line at least 1.5 pt above the page's body size becomes ``## heading``.
- Scanned pages. A page with fewer than MIN_TEXT_CHARS extractable characters has no
  text layer. With OCR_ENABLED it is rendered (pypdfium2, via pdfplumber: no poppler
  needed) and read by Tesseract; the page is marked ``ocr=True``.

Every page keeps its 1-based number in the file, which becomes chunk metadata.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Iterable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .config import IndexerSettings, get_settings
from .models import Document, Page

SUPPORTED_SUFFIXES = {".pdf", ".md", ".markdown", ".html", ".htm", ".txt"}

# Layout constants, tuned on the real corpus (see docs/PIPELINE.md, "Extraction").
HEADER_FOOTER_BAND = 0.07   # top and bottom 7% of a page: running headers and footers
MIN_GUTTER_WIDTH = 7.0      # points of empty space between columns (prospectus: ~9 pt)
MIN_COLUMN_SHARE = 0.18     # each column spans at least 18% of the text width
HEADING_SIZE_DELTA = 1.5    # points above body size for a line to count as a heading
MAX_HEADING_WORDS = 14
MAX_OCR_PIXELS = 40_000_000  # render cap per page for OCR (about 40 MB greyscale)
OCR_TIMEOUT_SECONDS = 180    # one page; a scan takes a few seconds on a laptop CPU


class ExtractionError(RuntimeError):
    """This document cannot be extracted; the message says which file and why."""


def extract(path: str, doc_version: str, superseded: bool,
            settings: IndexerSettings | None = None) -> Document:
    """Return the document's text page by page.

    Detect pages with no text layer and OCR them; mark those pages ocr=True.
    OCR needs the tesseract binary (tesseract-ocr in the indexer Dockerfile)."""
    settings = settings or get_settings()
    file = Path(path)
    if not file.is_file():
        raise ExtractionError(f"{file}: file not found")
    suffix = file.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ExtractionError(f"{file.name}: unsupported file type {suffix!r}")
    stats: Counter[str] = Counter()
    if suffix == ".pdf":
        pages = _extract_pdf(file, settings, stats)
    elif suffix in {".html", ".htm"}:
        pages = [Page(file.name, 1, html_to_text(_read_text(file)))]
    elif suffix in {".md", ".markdown"}:
        pages = [Page(file.name, 1, strip_front_matter(_read_text(file)))]
    else:
        pages = [Page(file.name, 1, _read_text(file))]
    stats["pages"] = len(pages)
    return Document(source=file.name, doc_version=doc_version, superseded=superseded,
                    pages=pages, stats=dict(stats))


def _read_text(file: Path) -> str:
    try:
        return file.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return file.read_text(encoding="cp1252", errors="replace")


# ---------------------------------------------------------------------------- Markdown
def strip_front_matter(text: str) -> str:
    """Drop a leading ``---`` block (the fixtures keep doc_version there)."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                return "\n".join(lines[i + 1:]).strip() + "\n"
    return text


# -------------------------------------------------------------------------------- HTML
class _HtmlToText(HTMLParser):
    # <head>/<title> too: the real Fee Structure page is titled "Accreditation".
    SKIP = {"head", "title", "script", "style", "noscript", "nav", "header", "footer", "form",
            "svg", "button", "select", "iframe", "template"}
    VOID = {"br", "img", "input", "meta", "link", "hr", "source", "wbr", "area", "col"}
    BLOCK = {"p", "div", "section", "article", "main", "ul", "ol", "li", "dl", "dt", "dd",
             "blockquote", "figure", "figcaption", "aside", "span_block"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.buf: list[str] = []
        self.skip_depth = 0
        self.heading: int | None = None
        self.table_depth = 0                    # >1: a table nested inside a cell
        self.rows: list[list[str]] | None = None
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.colspan = 1

    def _flush(self, prefix: str = "") -> None:
        text = " ".join(" ".join(self.buf).split())
        self.buf = []
        if text:
            self.lines.append(prefix + text)

    def _close_cell(self) -> None:
        if self.cell is not None and self.row is not None:
            # a colspan cell keeps its columns aligned: its value, then empty cells
            self.row += [" ".join(" ".join(self.cell).split())] + [""] * (self.colspan - 1)
        self.cell = None
        self.colspan = 1

    def _close_row(self) -> None:
        self._close_cell()
        if self.row is not None and self.rows is not None:
            self.rows.append(self.row)
        self.row = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.VOID:
            if tag == "br" and not self.skip_depth:
                (self.cell if self.cell is not None else self.buf).append(" ")
                if self.cell is None:
                    self._flush()
            return
        if tag in self.SKIP:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self.rows is None:
            self._flush()
            self.heading = int(tag[1])
        elif tag == "table":
            self.table_depth += 1
            if self.table_depth == 1:
                self._flush()
                self.rows = []
            elif self.cell is not None:
                self.cell.append(" ")           # nested table: its text joins the outer cell
        elif self.table_depth > 1:
            if tag in {"td", "th", "tr"} and self.cell is not None:
                self.cell.append(" ")
        elif tag == "tr" and self.rows is not None:
            self._close_row()                   # </tr> may be omitted in valid HTML
            self.row = []
        elif tag in {"td", "th"} and self.rows is not None:
            self._close_cell()                  # </td> may be omitted too
            if self.row is None:
                self.row = []                   # a cell with no <tr> (also legal)
            self.cell = []
            span = dict(attrs).get("colspan") or "1"
            self.colspan = max(1, min(int(span), 20)) if span.strip().isdigit() else 1
        elif tag in self.BLOCK and self.rows is None:
            self._flush()
            if tag == "li":
                self.buf.append("-")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self.heading is not None:
            text = " ".join(" ".join(self.buf).split())
            self.buf = []
            if text:
                self.lines += ["", "#" * self.heading + " " + text, ""]
            self.heading = None
        elif tag == "table" and self.table_depth > 1:
            self.table_depth -= 1
        elif tag in {"td", "th", "tr"} and self.table_depth > 1:
            pass                                 # a nested table stays inside its cell
        elif tag in {"td", "th"}:
            self._close_cell()
        elif tag == "tr":
            self._close_row()
        elif tag == "table" and self.rows is not None:
            self._close_row()
            self.table_depth = 0
            table = render_table(self.rows)
            self.rows = None
            if table:
                self.lines += ["", table, ""]
        elif tag in self.BLOCK and self.rows is None:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self.skip_depth or not data.strip():
            return
        # Text inside a table but outside any cell (a <caption>) goes to the prose.
        (self.cell if self.cell is not None else self.buf).append(data.strip())

    def text(self) -> str:
        self._flush()
        return _collapse_blank_lines(self.lines)


def html_to_text(html: str) -> str:
    """Headings become ``#`` lines, tables become pipe rows; navigation, headers,
    footers, forms and scripts are dropped."""
    parser = _HtmlToText()
    parser.feed(html)
    parser.close()
    return parser.text()


# ------------------------------------------------------------------------------ Tables
def render_table(rows: Iterable[Iterable[str | None]]) -> str:
    """Pipe rows, one per table row. Empty rows and empty columns are dropped; a table
    smaller than 2x2 after that is not a table and renders as nothing."""
    cleaned = [[" ".join((c or "").split()).replace("|", "/") for c in r] for r in rows]
    cleaned = [r for r in cleaned if any(r)]
    if not cleaned:
        return ""
    width = max(len(r) for r in cleaned)
    cleaned = [r + [""] * (width - len(r)) for r in cleaned]
    keep = [j for j in range(width) if any(r[j] for r in cleaned)]
    cleaned = [[r[j] for j in keep] for r in cleaned]
    if len(cleaned) < 2 or len(keep) < 2:
        return ""
    return "\n".join("| " + " | ".join(r) + " |" for r in cleaned)


def _real_tables(page: Any) -> list[tuple[tuple[float, float, float, float], str]]:
    px0, ptop, px1, pbottom = page.bbox
    width, height = px1 - px0, pbottom - ptop
    found = []
    for table in page.find_tables():
        x0, top, x1, bottom = table.bbox
        if (x1 - x0) * (bottom - top) > 0.85 * width * height:
            continue                                    # a frame around the whole page
        if bottom <= ptop + 0.1 * height or top >= pbottom - 0.1 * height:
            continue                                    # a header or footer box
        rows = table.extract()
        cells = [c for r in rows for c in r]
        filled = sum(1 for c in cells if c and c.strip())
        if not cells or filled / len(cells) < 0.5:
            continue
        rendered = render_table(rows)
        if rendered:
            found.append(((x0, top, x1, bottom), rendered))
    return found


def _inside(obj: dict[str, Any], boxes: list[tuple[float, float, float, float]]) -> bool:
    cx = (obj["x0"] + obj["x1"]) / 2
    cy = (obj["top"] + obj["bottom"]) / 2
    return any(x0 <= cx <= x1 and top <= cy <= bottom for x0, top, x1, bottom in boxes)


# ------------------------------------------------------------------------------ Layout
def find_gutters(words: list[dict[str, Any]], top: float, bottom: float) -> list[float]:
    """x positions of empty vertical strips that split the body text into columns.

    Only the body (outside the header/footer bands) is measured, so a running header
    that spans the page does not hide the gutter. A strip counts when almost no word
    crosses it, it is at least MIN_GUTTER_WIDTH wide, and every resulting column is at
    least MIN_COLUMN_SHARE of the text width (so the gap between a clause number and
    its text, or between table cells, is not mistaken for a gutter)."""
    band = (bottom - top) * HEADER_FOOTER_BAND
    body = [w for w in words if w["top"] > top + band and w["bottom"] < bottom - band]
    if len(body) < 30:
        return []
    left = min(w["x0"] for w in body)
    right = max(w["x1"] for w in body)
    span = right - left
    if span < 150:
        return []
    step = 2.0
    bins = int(span / step) + 1
    cover = [0] * bins
    for w in body:
        for b in range(int((w["x0"] - left) / step), min(bins, int((w["x1"] - left) / step) + 1)):
            cover[b] += 1
    tolerance = max(1, len(body) // 200)
    candidates: list[tuple[float, float]] = []
    i = 0
    while i < bins:
        if cover[i] > tolerance:
            i += 1
            continue
        j = i
        while j < bins and cover[j] <= tolerance:
            j += 1
        x0, x1 = left + i * step, left + j * step
        if x1 - x0 >= MIN_GUTTER_WIDTH:
            candidates.append((x0, x1))
        i = j
    gutters: list[float] = []
    edge = left
    for x0, x1 in candidates:
        if x0 - edge >= MIN_COLUMN_SHARE * span and right - x1 >= MIN_COLUMN_SHARE * span:
            gutters.append((x0 + x1) / 2)
            edge = x1
    return gutters


def _bands(words: list[dict[str, Any]], gutters: list[float], top: float,
           bottom: float) -> list[tuple[float, float, bool]]:
    """Horizontal bands, top to bottom: (y0, y1, spanning). A spanning band holds
    lines that cross a gutter and is read across the full width."""
    if not gutters:
        return [(top, bottom, True)]
    spans = sorted((w["top"] - 1, w["bottom"] + 1) for w in words
                   if any(w["x0"] < g - 1 and w["x1"] > g + 1 for g in gutters))
    merged: list[list[float]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    bands: list[tuple[float, float, bool]] = []
    cursor = top
    for a, b in merged:
        a, b = max(a, top), min(b, bottom)
        if a > cursor:
            bands.append((cursor, a, False))
        bands.append((a, b, True))
        cursor = max(cursor, b)
    if cursor < bottom:
        bands.append((cursor, bottom, False))
    return [band for band in bands if band[1] - band[0] > 0.5]


def _table_bands(boxes: list[tuple[float, float, float, float]], top: float,
                 bottom: float) -> list[tuple[float, float, bool]]:
    """Single-column page: cut the text at each table, so a table is read where it
    sits on the page rather than after the footer."""
    bands: list[tuple[float, float, bool]] = []
    cursor = top
    for _, t_top, _, t_bottom in sorted(boxes, key=lambda b: b[1]):
        if t_top > cursor:
            bands.append((cursor, t_top, True))
        cursor = max(cursor, t_bottom)
    bands.append((cursor, bottom, True))
    return [band for band in bands if band[1] - band[0] > 0.5]


def _body_style(words: list[dict[str, Any]]) -> tuple[float, str]:
    """The most common font size and font name on the page: what body text looks like."""
    sizes = Counter(round(float(w.get("size", 0)) * 2) / 2 for w in words)
    fonts = Counter(_font(w.get("fontname", "")) for w in words)
    return (sizes.most_common(1)[0][0] if sizes else 0.0,
            fonts.most_common(1)[0][0] if fonts else "")


def _font(name: str) -> str:
    return str(name).split("+")[-1]       # drop the subset prefix ("ABCDEF+FiraSans-Light")


def _heading_shape(text: str) -> bool:
    letters = sum(ch.isalpha() for ch in text)
    return (letters >= 3 and len(text.split()) <= MAX_HEADING_WORDS
            and text.rstrip()[-1:] not in {",", ".", ";", ":"})


_WEIGHT_NAMES = (("extralight", 2), ("ultralight", 2), ("semibold", 6), ("demibold", 6),
                 ("extrabold", 8), ("ultrabold", 8), ("hairline", 1), ("thin", 1),
                 ("light", 3), ("medium", 5), ("black", 9), ("heavy", 9), ("bold", 7))


def font_weight(font: str) -> int:
    """Weight of a font from its name, 1 (thin) to 9 (black); 4 = regular."""
    name = re.sub(r"[-_ ]", "", font.lower())
    for word, weight in _WEIGHT_NAMES:
        if word in name:
            return weight
    return 4


def _heading_level(text: str, chars: list[dict[str, Any]], body_size: float,
                   body_font: str) -> int:
    """0 = not a heading. Two signals, both measured on the corpus:
    - larger than body text (handbooks: 12 pt headings over 10 pt body) -> level 2,
      or level 1 when at least 4 pt larger;
    - same size but a heavier weight for almost the whole line (prospectus:
      FiraSans-Regular headings over FiraSans-Light body) -> level 2. A different but
      not heavier face (italic, another family) is not a heading."""
    if not chars or not _heading_shape(text):
        return 0
    size = statistics.median(float(c.get("size", 0)) for c in chars)
    if body_size > 0 and size >= body_size + HEADING_SIZE_DELTA:
        return 1 if size >= body_size + 4 else 2
    fonts = Counter(_font(c.get("fontname", "")) for c in chars if str(c.get("text", "")).strip())
    if body_font and fonts:
        font, count = fonts.most_common(1)[0]
        if (font_weight(font) > font_weight(body_font) and count >= 0.8 * sum(fonts.values())
                and len(text.split()) <= 10):
            return 2
    return 0


def _render_lines(lines: list[dict[str, Any]], body_size: float, body_font: str) -> list[str]:
    lines = [ln for ln in lines if " ".join(str(ln["text"]).split())]
    texts = [" ".join(str(ln["text"]).split()) for ln in lines]
    levels = [_heading_level(t, ln.get("chars") or [], body_size, body_font)
              for t, ln in zip(texts, lines)]
    # A "heading" whose sentence runs on into an ordinary next line is a sentence
    # ("The following letter grades are" / "common to all degree programs:").
    for i in range(len(levels) - 1):
        if levels[i] and not levels[i + 1] and texts[i + 1][:1].islower():
            levels[i] = 0
    out: list[str] = []
    previous: dict[str, Any] | None = None
    previous_heading: tuple[int, int] | None = None     # (index in out, level)
    for line, text, level in zip(lines, texts, levels):
        gap = line["top"] - previous["bottom"] if previous is not None else 0.0
        paragraph_break = previous is not None and \
            gap > max(4.0, previous["bottom"] - previous["top"])
        if level and previous_heading and previous_heading[1] == level and not paragraph_break:
            index = previous_heading[0]                 # a heading wrapped onto two lines
            out[index] = f"{out[index]} {text}"
        elif level:
            out += ["", "#" * level + " " + text, ""]
            previous_heading = (len(out) - 2, level)
        else:
            if paragraph_break:
                out.append("")                          # vertical gap: new paragraph
            out.append(text)
            previous_heading = None
        previous = line
    return out


def _visible_box(page: Any) -> tuple[float, float, float, float]:
    """The part of the page a viewer shows: the CropBox, clipped to the page's box.
    (In this corpus the two are equal; a PDF may hide content through its CropBox.)"""
    px0, ptop, px1, pbottom = page.bbox
    crop = getattr(page, "cropbox", None) or page.bbox
    x0, top, x1, bottom = max(px0, crop[0]), max(ptop, crop[1]), min(px1, crop[2]), \
        min(pbottom, crop[3])
    return (x0, top, x1, bottom) if x1 > x0 and bottom > top else (px0, ptop, px1, pbottom)


def pdf_page_text(page: Any, stats: Counter[str] | None = None) -> str:
    """One PDF page with a text layer, read column by column, as light Markdown."""
    stats = stats if stats is not None else Counter()
    # Only what is inside the page's visible box. The 2026 handbooks store the facing
    # printed page's text outside each page's box; extract_text() returns it too,
    # interleaved with the real page, so every clause would be indexed twice and
    # under the wrong page number. within_bbox keeps objects wholly inside the box.
    visible = page.within_bbox(_visible_box(page))
    if len(visible.chars) < len(page.chars):
        stats["pages_with_offpage_text"] += 1
    page = visible
    px0, ptop, px1, pbottom = page.bbox
    words = page.extract_words(extra_attrs=["size", "fontname"])
    if not words:
        return ""
    tables = _real_tables(page)
    boxes = [bbox for bbox, _ in tables]
    stats["tables"] += len(tables)
    words = [w for w in words if not _inside(w, boxes)]
    body_size, body_font = _body_style(words)
    gutters = find_gutters(words, ptop, pbottom)
    if gutters:
        stats["multi_column_pages"] += 1
    edges = [px0, *gutters, px1]
    text_only = page.filter(lambda o: o.get("object_type") != "char" or not _inside(o, boxes)) \
        if boxes else page

    blocks: list[tuple[tuple[int, int, float], list[str]]] = []
    bands = _bands(words, gutters, ptop, pbottom) if gutters else _table_bands(boxes, ptop, pbottom)
    for b_index, (y0, y1, spanning) in enumerate(bands):
        columns = [(px0, px1)] if spanning else list(zip(edges[:-1], edges[1:]))
        for c_index, (x0, x1) in enumerate(columns):
            x0, x1 = max(x0, px0), min(x1, px1)
            if x1 - x0 < 1 or y1 - y0 < 0.5:
                continue
            region = text_only.within_bbox((x0, max(y0, ptop), x1, min(y1, pbottom)))
            lines = region.extract_text_lines(return_chars=True, strip=True)
            if lines:
                blocks.append(((b_index, c_index, y0), _render_lines(lines, body_size, body_font)))
    for bbox, rendered in tables:
        x_mid = (bbox[0] + bbox[2]) / 2
        # The band the table starts in; on a single-column page the table sits in the
        # gap between two bands, so it sorts after the band above it.
        b_index = next((i for i, (y0, y1, _) in enumerate(bands) if y0 <= bbox[1] < y1),
                       sum(1 for y0, _, _ in bands if y0 < bbox[1]) - 1 if bands else 0)
        c_index = next((i for i, (x0, x1) in enumerate(zip(edges[:-1], edges[1:]))
                        if x0 <= x_mid < x1), 0)
        if b_index < len(bands) and bands[b_index][2]:
            c_index = 0
        blocks.append(((b_index, c_index, bbox[1]), ["", rendered, ""]))
    blocks.sort(key=lambda block: block[0])
    lines_out: list[str] = []
    for _, lines in blocks:
        lines_out += lines + [""]
    return _collapse_blank_lines(lines_out)


# --------------------------------------------------------------------------------- OCR
_NUMBERED_HEADING = re.compile(r"^(\d{1,2}|[IVXL]{1,5})[.)-]?\s+[A-Z][A-Za-z/&,()' -]{2,}$")


def _ocr_heading(line: str) -> bool:
    """OCR text has no font sizes, so headings are recognised from their shape:
    a short line with no closing punctuation that is either numbered like
    '5. Academic Warning' or written in capitals."""
    words = line.split()
    if not words or len(words) > 10 or line.rstrip()[-1:] in ".,;:":
        return False
    letters = [ch for ch in line if ch.isalpha()]
    if len(letters) < 4:
        return False
    if _NUMBERED_HEADING.match(line) and not re.match(r"^\d+\.\d", line):
        return True
    return sum(ch.isupper() for ch in letters) / len(letters) > 0.8


def ocr_text_to_markdown(text: str) -> str:
    out = []
    for raw in text.splitlines():
        line = " ".join(raw.split())
        out += ["", "## " + line, ""] if _ocr_heading(line) else [line]
    return _collapse_blank_lines(out)


def ocr_page(page: Any, settings: IndexerSettings) -> str:
    """Render one pdfium page and read it with Tesseract. One page at a time, released
    right after: a 300 dpi A4 page is about 9 MB as greyscale, a spread twice that."""
    try:
        import pytesseract
    except ImportError as exc:  # pragma: no cover - it is in requirements.txt
        raise ExtractionError("pytesseract is not installed") from exc
    if settings.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd
    width, height = page.get_size()            # points
    # An untrusted PDF can declare a huge page; cap the render at MAX_OCR_PIXELS so
    # one page cannot exhaust the Job's memory (A4 at 300 dpi is about 8.7 Mpx).
    scale = min(settings.ocr_dpi / 72, (MAX_OCR_PIXELS / max(width * height, 1.0)) ** 0.5)
    bitmap = page.render(scale=scale, grayscale=True)
    image = bitmap.to_pil()
    bitmap.close()
    try:
        text = pytesseract.image_to_string(image, lang=settings.ocr_lang, config="--psm 3",
                                           timeout=OCR_TIMEOUT_SECONDS)
    except RuntimeError as exc:                # pytesseract's timeout
        raise ExtractionError(f"OCR timed out after {OCR_TIMEOUT_SECONDS}s on one page") \
            from exc
    except pytesseract.TesseractNotFoundError as exc:
        raise ExtractionError(
            "OCR is enabled but the tesseract binary was not found. Install tesseract-ocr "
            "(the indexer Dockerfile does), or set TESSERACT_CMD to its path. Set "
            "OCR_ENABLED=false only to skip scanned pages deliberately.") from exc
    finally:
        image.close()
    return ocr_text_to_markdown(text)


# --------------------------------------------------------------------------------- PDF
def _text_layer_chars(page: Any) -> int:
    """Non-space characters in a page's text layer, read with pdfium. Fast: it does not
    lay out the page. The Aug 2023 rules are text converted to vector outlines (about
    3,000 paths per page, no characters); pdfplumber spends ~4 s per page parsing those
    paths, so pages without a text layer never reach pdfplumber at all."""
    textpage = page.get_textpage()
    try:
        return len(re.sub(r"\s", "", textpage.get_text_range()))
    finally:
        textpage.close()


def _extract_pdf(file: Path, settings: IndexerSettings, stats: Counter[str]) -> list[Page]:
    import pdfplumber
    import pypdfium2

    try:
        pdf = pdfplumber.open(file)
    except Exception as exc:
        raise ExtractionError(f"{file.name}: not a readable PDF ({type(exc).__name__}: {exc})") \
            from exc
    try:
        pdfium = pypdfium2.PdfDocument(str(file))
    except Exception as exc:
        pdf.close()
        raise ExtractionError(f"{file.name}: not a readable PDF ({type(exc).__name__}: {exc})") \
            from exc
    pages: list[Page] = []
    try:
        for index, page in enumerate(pdf.pages):
            number = index + 1
            fast = pdfium[index]
            try:
                text = pdf_page_text(page, stats) \
                    if _text_layer_chars(fast) >= settings.min_text_chars else ""
                ocr = False
                if len(re.sub(r"\s", "", text)) < settings.min_text_chars:
                    stats["pages_without_text_layer"] += 1
                    if settings.ocr_enabled:
                        text, ocr = ocr_page(fast, settings), True
                        stats["ocr_pages"] += 1
                    else:
                        stats["ocr_skipped_pages"] += 1
            except ExtractionError:
                raise
            except Exception as exc:
                raise ExtractionError(f"{file.name} page {number}: {type(exc).__name__}: {exc}") \
                    from exc
            finally:
                fast.close()
                page.close()              # free this page's parsed objects and caches
            pages.append(Page(file.name, number, text, ocr))
    finally:
        pdfium.close()
        pdf.close()
    return pages


# ------------------------------------------------------------------------------ shared
def _collapse_blank_lines(lines: list[str]) -> str:
    out: list[str] = []
    for line in lines:
        line = line.rstrip()
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out) + ("\n" if out else "")
