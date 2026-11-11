"""Stage 1, extraction: every layout trap found in the real corpus, on generated files."""

from __future__ import annotations

import os
import shutil
from dataclasses import replace
from pathlib import Path

import pdfgen
import pytest

from pipeline import extract as extract_module
from pipeline.config import get_settings
from pipeline.extract import (
    ExtractionError,
    extract,
    find_gutters,
    html_to_text,
    ocr_text_to_markdown,
    render_table,
    strip_front_matter,
)

FIXTURES = Path(__file__).resolve().parents[2] / "eval" / "fixtures"


def _settings(**overrides: object):  # type: ignore[no-untyped-def]
    return replace(get_settings(), **overrides)


# ------------------------------------------------------------------------- PDF, text
def test_pdf_pages_keep_their_numbers_headings_clauses_and_table(tmp_path: Path) -> None:
    doc = extract(str(pdfgen.handbook_pdf(tmp_path / "h.pdf")), "2026-06", False,
                  _settings(ocr_enabled=False))
    assert [p.page for p in doc.pages] == [1, 2, 3]
    assert doc.source == "h.pdf" and doc.doc_version == "2026-06" and not doc.superseded
    first = doc.pages[0].text
    assert "## Attendance Requirements" in first
    assert "1.21 Students are required to maintain 100% attendance" in first
    assert "| Grade | Points |" in first and "| A+ | 4.00 |" in first and "| D | 1.00 |" in first
    assert "## Academic Warning" in doc.pages[1].text
    assert "5.4 A student whose CGPA falls below 2.00" in doc.pages[1].text
    assert doc.stats["tables"] == 1 and doc.stats["pages"] == 3


def test_table_cells_are_not_repeated_as_prose(tmp_path: Path) -> None:
    text = extract(str(pdfgen.handbook_pdf(tmp_path / "h.pdf")), "v", False,
                   _settings(ocr_enabled=False)).pages[0].text
    prose = [ln for ln in text.splitlines() if not ln.startswith("|")]
    assert not any(ln.strip() in {"A+", "4.00", "A+ 4.00"} for ln in prose)


def test_two_columns_are_read_one_after_the_other(tmp_path: Path) -> None:
    doc = extract(str(pdfgen.two_column_pdf(tmp_path / "c.pdf")), "v", False,
                  _settings(ocr_enabled=False))
    text = doc.pages[0].text
    assert doc.stats["multi_column_pages"] == 1
    # every left-column word comes before the first right-column word
    assert text.rindex("LEFT-END") < text.index("RIGHT-START")
    flat = " ".join(text.split())          # extraction keeps the PDF's line wrapping
    assert "describes registration" in flat and "describes withdrawal" in flat


def test_text_outside_the_page_box_is_not_extracted(tmp_path: Path) -> None:
    doc = extract(str(pdfgen.offpage_pdf(tmp_path / "o.pdf")), "v", False,
                  _settings(ocr_enabled=False))
    text = doc.pages[0].text
    assert "VISIBLE clause 1.9" in text and "VISIBLE clause 1.10" in text
    assert "HIDDEN" not in text
    assert doc.stats["pages_with_offpage_text"] == 1


def test_heavier_face_at_body_size_is_a_heading(tmp_path: Path) -> None:
    text = extract(str(pdfgen.weight_heading_pdf(tmp_path / "w.pdf")), "v", False,
                   _settings(ocr_enabled=False)).pages[0].text
    assert "## 1. Attendance Requirement" in text
    assert "## 2. Medium of Instruction" in text
    assert "## Students must attend" not in text


def test_a_different_but_not_heavier_face_is_not_a_heading(tmp_path: Path) -> None:
    text = extract(str(pdfgen.style_pdf(tmp_path / "s.pdf")), "v", False,
                   _settings(ocr_enabled=False)).pages[0].text
    assert "## An Italic Remark" not in text and "An Italic Remark" in text
    # bold, but its sentence runs on into the next line: a sentence, not a heading
    assert "## The following letter grades are" not in text
    assert "The following letter grades are" in text


def test_mixed_document_ocrs_only_the_scanned_page(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake_ocr(page: object, settings: object) -> str:
        calls.append(1)
        return "Recognised scanned text on this page.\n"

    monkeypatch.setattr(extract_module, "ocr_page", fake_ocr)
    doc = extract(str(pdfgen.mixed_pdf(tmp_path / "m.pdf")), "v", False, _settings())
    assert [p.page for p in doc.pages] == [1, 2, 3]
    assert [p.ocr for p in doc.pages] == [False, True, False]
    assert calls == [1]
    assert "Text page 3." in doc.pages[2].text


def test_gutter_finder_ignores_narrow_gaps() -> None:
    def word(x0: float, x1: float, top: float) -> dict[str, float]:
        return {"x0": x0, "x1": x1, "top": top, "bottom": top + 10}

    # clause numbers at x=40 with text from x=80: a gap, but not a column
    single = [word(40, 60, 100 + i * 12) for i in range(20)] + \
             [word(80, 500, 100 + i * 12) for i in range(20)]
    assert find_gutters(single, 0, 800) == []
    double = [word(40, 280, 100 + i * 12) for i in range(20)] + \
             [word(300, 540, 100 + i * 12) for i in range(20)]
    assert len(find_gutters(double, 0, 800)) == 1


# --------------------------------------------------------------------- PDF, scanned
def test_page_without_text_layer_goes_to_ocr(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake_ocr(page: object, settings: object) -> str:
        calls.append(1)
        return "## 5. Academic Warning\n\nA student whose CGPA falls below 1.75 is warned.\n"

    monkeypatch.setattr(extract_module, "ocr_page", fake_ocr)
    doc = extract(str(pdfgen.scanned_pdf(tmp_path / "s.pdf")), "2023-08", True, _settings())
    assert calls == [1]
    assert doc.pages[0].ocr is True
    assert "below 1.75" in doc.pages[0].text
    assert doc.stats["pages_without_text_layer"] == 1 and doc.stats["ocr_pages"] == 1


def test_ocr_disabled_leaves_scanned_page_empty_and_counts_it(tmp_path: Path) -> None:
    doc = extract(str(pdfgen.scanned_pdf(tmp_path / "s.pdf")), "2023-08", True,
                  _settings(ocr_enabled=False))
    assert doc.pages[0].text.strip() == "" and doc.pages[0].ocr is False
    assert doc.stats["ocr_skipped_pages"] == 1


def test_missing_tesseract_is_a_clear_error(tmp_path: Path) -> None:
    missing = str(tmp_path / "no-such-tesseract.exe")
    with pytest.raises(ExtractionError, match="tesseract binary was not found"):
        extract(str(pdfgen.scanned_pdf(tmp_path / "s.pdf")), "v", True,
                _settings(tesseract_cmd=missing))


def _tesseract() -> str | None:
    configured = os.environ.get("TESSERACT_CMD")
    if configured and Path(configured).exists():
        return configured
    return shutil.which("tesseract")


@pytest.mark.skipif(_tesseract() is None, reason="tesseract binary not installed")
def test_real_ocr_reads_the_scanned_page(tmp_path: Path) -> None:
    doc = extract(str(pdfgen.scanned_pdf(tmp_path / "s.pdf")), "2023-08", True,
                  _settings(tesseract_cmd=_tesseract() or ""))
    text = doc.pages[0].text
    assert doc.pages[0].ocr is True
    assert "1.75" in text and "academic warning" in text.lower()


def test_ocr_text_headings_are_recognised_by_shape() -> None:
    text = ocr_text_to_markdown("ACADEMIC RULES\n5. Academic Warning\n"
                                "5.4 A student whose CGPA is below 2.00 is warned.\n"
                                "This sentence ends with a period.")
    assert "## ACADEMIC RULES" in text and "## 5. Academic Warning" in text
    assert "## 5.4" not in text and "## This sentence" not in text


# --------------------------------------------------------------- Markdown, HTML, text
def test_markdown_front_matter_is_stripped_and_structure_kept() -> None:
    doc = extract(str(FIXTURES / "fees-2026.md"), "2026", False, _settings())
    assert len(doc.pages) == 1 and doc.pages[0].page == 1
    text = doc.pages[0].text
    assert not text.startswith("---") and "doc_version" not in text
    assert "## 1. Tuition" in text and "| BS (all disciplines) | 10,000 |" in text


def test_strip_front_matter_leaves_plain_markdown_alone() -> None:
    assert strip_front_matter("# Title\n---\nbody\n") == "# Title\n---\nbody\n"


def test_html_keeps_headings_and_tables_and_drops_navigation() -> None:
    html = """<html><head><title>Wrong title</title><style>.x{}</style></head><body>
    <header><a href="/">Home</a> Menu</header><nav><ul><li>Admissions</li></ul></nav>
    <h2>Fee Refund Policy</h2><p>Tuition is refunded as follows &amp; no later.</p>
    <table><tr><th>Timeline</th><th>Refund</th></tr>
    <tr><td>Up to 10th day</td><td>100%</td></tr><tr><td>31st day onwards</td><td>None</td></tr>
    </table><script>var secret = 1;</script><footer>Copyright</footer></body></html>"""
    text = html_to_text(html)
    assert "## Fee Refund Policy" in text
    assert "Tuition is refunded as follows & no later." in text
    assert "| Timeline | Refund |" in text and "| Up to 10th day | 100% |" in text
    for leaked in ("Home", "Menu", "Admissions", "secret", "Copyright", "Wrong title"):
        assert leaked not in text


def test_html_nested_tables_colspan_and_cell_line_breaks_lose_nothing() -> None:
    html = """<table>
    <tr><th>Charge</th><th>Amount</th></tr>
    <tr><td colspan="2">Fees for the 2026-27 academic year</td></tr>
    <tr><td>Hostel&nbsp;deposit</td><td>Rs.&nbsp;20,000<br>refundable</td></tr>
    <tr><td>Breakdown</td><td><table><tr><td>x</td><td>y</td></tr><tr><td>z</td></tr></table>w</td></tr>
    <tr><td>Library</td><td><ul><li>fine</li><li>per day</li></ul></td></tr>
    </table>"""
    text = html_to_text(html)
    assert "| Charge | Amount |" in text
    assert "| Fees for the 2026-27 academic year |  |" in text      # colspan padded
    assert "| Hostel deposit | Rs. 20,000 refundable |" in text
    assert "| Breakdown | x y z w |" in text                       # nested table kept
    assert "| Library | fine per day |" in text


def test_html_with_omitted_end_tags_still_gives_the_table() -> None:
    # </td> and </tr> are optional in HTML
    text = html_to_text("<table><tr><th>Grade<th>Points<tr><td>A+<td>4.00"
                        "<tr><td>B<td>3.00</table><p>After the table.</p>")
    assert text.startswith("| Grade | Points |\n| A+ | 4.00 |\n| B | 3.00 |")
    assert "After the table." in text


def test_non_utf8_text_file_falls_back_to_cp1252(tmp_path: Path) -> None:
    legacy = tmp_path / "old.txt"
    legacy.write_bytes("Café fee – Rs. 500".encode("cp1252"))
    assert extract(str(legacy), "v", False, _settings()).pages[0].text == \
        "Café fee – Rs. 500"


def test_html_file_and_text_file(tmp_path: Path) -> None:
    page = tmp_path / "fees.html"
    page.write_text("<h1>Fees</h1><p>Admission fee Rs 30,000.</p>", encoding="utf-8")
    doc = extract(str(page), "2026", False, _settings())
    assert doc.pages[0].text.startswith("# Fees") and "Rs 30,000" in doc.pages[0].text
    note = tmp_path / "note.txt"
    note.write_text("Plain policy text.", encoding="utf-8")
    assert extract(str(note), "v", False, _settings()).pages[0].text == "Plain policy text."


def test_render_table_drops_empty_rows_columns_and_non_tables() -> None:
    rows = [["", "", ""], ["Grade", None, "Points"], ["A|+", "", "4.00"], [None, None, None]]
    assert render_table(rows) == "| Grade | Points |\n| A/+ | 4.00 |"
    assert render_table([["only one row", "x"]]) == ""
    assert render_table([["a"], ["b"]]) == ""


# ------------------------------------------------------------------------- failures
def test_unsupported_missing_and_corrupt_files(tmp_path: Path) -> None:
    with pytest.raises(ExtractionError, match="file not found"):
        extract(str(tmp_path / "none.pdf"), "v", False, _settings())
    other = tmp_path / "sheet.xlsx"
    other.write_bytes(b"x")
    with pytest.raises(ExtractionError, match="unsupported file type"):
        extract(str(other), "v", False, _settings())
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 this is not really a pdf")
    with pytest.raises(ExtractionError, match="broken.pdf"):
        extract(str(broken), "v", False, _settings())
