"""Small real PDFs that reproduce the layout traps found in the FAST-NUCES corpus.

Built at test time with fpdf2 (text layer, fonts, ruled tables) and Pillow (an
image-only page), so the tests need no downloaded documents.
"""

from __future__ import annotations

from pathlib import Path

from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFont

HEADER = "MARGALLA INSTITUTE OF TECHNOLOGY"
FOOTER = "Academic Rules and Regulations 2026"

LONG_DEFINITION = (
    "Generative AI means any software system that produces text, images, code or other "
    "content in response to a prompt, including large language models and image models. "
    "Acceptable use means use that the course instructor has permitted in writing for a "
    "specific assessment, and that the student declares in the submission. Unacceptable use "
    "means submitting generated content as the student's own work, using a tool during an "
    "examination, or using a tool in any way the instructor has not permitted. A declaration "
    "must name the tool, the prompts used and the parts of the work it affected. Repeated "
    "unacceptable use is referred to the Disciplinary Committee, which may award a failing "
    "grade in the course. ")


def _page(pdf: FPDF, number: int) -> None:
    pdf.add_page()
    pdf.set_font("Helvetica", size=10)
    pdf.set_xy(15, 8)
    pdf.cell(0, 6, HEADER)
    pdf.set_xy(15, 284)
    pdf.cell(0, 6, f"{number}  {FOOTER}")
    pdf.set_xy(15, 22)


def _heading(pdf: FPDF, text: str, size: int = 13) -> None:
    pdf.set_font("Helvetica", size=size)
    pdf.ln(3)
    pdf.cell(0, 8, text, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=10)


def _para(pdf: FPDF, text: str) -> None:
    pdf.multi_cell(180, 5, text, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)


def handbook_pdf(path: Path) -> Path:
    """Three pages: running header/footer, 13 pt headings over 10 pt text, numbered
    clauses, a ruled table, and a long definition that needs more than one chunk."""
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(False)
    pdf.set_left_margin(15)
    _page(pdf, 1)
    _heading(pdf, "Attendance Requirements")
    _para(pdf, "1.21 Students are required to maintain 100% attendance in lectures and labs. "
               "Absence of at most 20% of the lectures may be condoned for genuine reasons.")
    _para(pdf, "1.22 A student who fails the attendance requirement receives the grade F/A.")
    _heading(pdf, "Grade Points")
    pdf.set_font("Helvetica", size=10)
    with pdf.table(width=100, col_widths=(50, 50), align="LEFT") as table:
        for row in (("Grade", "Points"), ("A+", "4.00"), ("B", "3.00"), ("C", "2.00"),
                    ("D", "1.00")):
            cells = table.row()
            for value in row:
                cells.cell(value)
    _page(pdf, 2)
    _heading(pdf, "Academic Warning")
    _para(pdf, "5.4 A student whose CGPA falls below 2.00 at the end of a regular semester is "
               "placed on academic warning.")
    _para(pdf, "5.5 A student on a second academic warning must not register for a new course.")
    _page(pdf, 3)
    _heading(pdf, "Policy on Generative AI Based Tools")
    _para(pdf, LONG_DEFINITION + LONG_DEFINITION)
    pdf.output(str(path))
    return path


def two_column_pdf(path: Path) -> Path:
    """One page, two text columns separated by a 12 mm gutter."""
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(False)
    pdf.add_page()
    pdf.set_font("Helvetica", size=10)
    left = ("LEFT-START The left column describes registration. A student registers for "
            "courses in the first week of the semester through the academic office. Late "
            "registration needs written approval and a fee. LEFT-END")
    right = ("RIGHT-START The right column describes withdrawal. A student may withdraw from a "
             "course until the twelfth week. A withdrawn course is shown with the grade W on "
             "the transcript. RIGHT-END")
    for x, text in ((15, left), (108, right)):
        pdf.set_xy(x, 30)
        pdf.multi_cell(87, 5, (text + " ") * 3)
    pdf.output(str(path))
    return path


def offpage_pdf(path: Path) -> Path:
    """A page whose content stream also holds text placed outside the page box, the way
    the 2026 handbooks carry the facing page's text."""
    pdf = FPDF(format="A5")
    pdf.set_auto_page_break(False)
    pdf.add_page()
    pdf.set_font("Helvetica", size=10)
    pdf.text(15, 30, "VISIBLE clause 1.9 A theory course carries 2 to 4 credit hours.")
    pdf.text(15, 40, "VISIBLE clause 1.10 A lab carries 1 or 2 credit hours.")
    pdf.text(170, 30, "HIDDEN clause 1.15 belongs to the facing page.")   # A5 is 148 mm wide
    pdf.output(str(path))
    return path


def weight_heading_pdf(path: Path) -> Path:
    """Headings in a heavier face at the same size as the body (the prospectus)."""
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(False)
    pdf.add_page()
    pdf.set_xy(15, 20)
    for heading, body in (("1. Attendance Requirement",
                           "Students must attend at least 80 percent of lectures."),
                          ("2. Medium of Instruction",
                           "The medium of instruction and examinations is English.")):
        pdf.set_font("Helvetica", style="B", size=10)
        pdf.cell(0, 6, heading, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=10)
        for _ in range(6):
            pdf.multi_cell(180, 5, body, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
    pdf.output(str(path))
    return path


def mixed_pdf(path: Path) -> Path:
    """Page 1 text, page 2 an image only (no text layer), page 3 text."""
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(False)
    for number in (1, 2, 3):
        pdf.add_page()
        if number == 2:
            image = Image.new("L", (600, 300), color=255)
            ImageDraw.Draw(image).text((40, 120), "SCANNED PAGE", fill=0,
                                       font=ImageFont.load_default(size=40))
            pdf.image(image, x=20, y=40, w=150)
        else:
            pdf.set_font("Helvetica", size=10)
            pdf.set_xy(15, 30)
            pdf.multi_cell(180, 5, f"Text page {number}. " * 12)
    pdf.output(str(path))
    return path


def style_pdf(path: Path) -> Path:
    """Body text, an italic line (a different but not heavier face) and a bold line
    whose sentence runs on into the next ordinary line."""
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(False)
    pdf.add_page()
    pdf.set_xy(15, 20)
    for _ in range(5):
        pdf.set_font("Helvetica", size=10)
        pdf.multi_cell(180, 5, "Body text about grades and their points.",
                       new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", style="I", size=10)
    pdf.cell(0, 5, "An Italic Remark", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", style="B", size=10)
    pdf.cell(0, 5, "The following letter grades are", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=10)
    pdf.cell(0, 5, "common to all degree programs.", new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(path))
    return path


def scanned_pdf(path: Path, text: str ="ACADEMIC RULES 2023\n\n5. Academic Warning\n\n"
                                           "A student whose CGPA falls below 1.75\nis placed "
                                           "on academic warning.") -> Path:
    """An image-only page: no text layer at all, like the Aug 2023 rules."""
    image = Image.new("L", (1240, 1754), color=255)        # A4 at 150 dpi
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=42)
    draw.multiline_text((120, 160), text, fill=0, font=font, spacing=24)
    image.save(str(path), "PDF", resolution=150.0)
    return path
