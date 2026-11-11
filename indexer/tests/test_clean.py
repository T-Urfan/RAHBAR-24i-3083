"""Stage 2, cleaning: artefacts go, policy content and structure stay."""

from __future__ import annotations

from pipeline.clean import clean
from pipeline.models import Document, Page


def _doc(*texts: str) -> Document:
    return Document("rules.pdf", "2026", False,
                    [Page("rules.pdf", i + 1, t) for i, t in enumerate(texts)], {"pages": 3})


def _page(n: int, body: str, footer_first: bool = False) -> str:
    footer = f"{n} Undergraduate Academic Rules 2026" if not footer_first else \
        f"Undergraduate Academic Rules 2026 {n}"
    return f"NATIONAL UNIVERSITY OF COMPUTER SCIENCES\n\n{body}\n\n{footer}\n"


def test_running_headers_and_footers_are_removed_whatever_the_page_number() -> None:
    doc = clean(_doc(_page(1, "1.1 First clause."), _page(2, "1.2 Second clause."),
                     _page(3, "1.3 Third clause.", footer_first=True),
                     _page(4, "## Heading\n\n1.4 Fourth clause.")))
    for page in doc.pages:
        assert "NATIONAL UNIVERSITY" not in page.text
        assert "Undergraduate Academic Rules" not in page.text
    assert doc.pages[0].text.strip() == "1.1 First clause."
    assert doc.stats["header_footer_lines_removed"] == 8


def test_header_marked_as_heading_on_a_cover_page_is_still_removed() -> None:
    pages = [_page(i, f"1.{i} Clause {i}.") for i in range(1, 4)]
    pages[0] = pages[0].replace("NATIONAL UNIVERSITY", "## NATIONAL UNIVERSITY")
    doc = clean(_doc(*pages))
    assert "NATIONAL UNIVERSITY" not in doc.pages[0].text


def test_a_line_repeated_only_in_the_body_is_kept() -> None:
    def page(n: int) -> str:
        lines = [f"Opening line {n}.", f"Second line {n}.", f"Third line {n}.",
                 "The fee is non-refundable.",            # same sentence, mid-page, every page
                 f"Fifth line {n}.", f"Sixth line {n}.", f"Closing line {n}."]
        return "\n".join(lines)

    doc = clean(_doc(page(1), page(2), page(3)))
    assert all("The fee is non-refundable." in p.text for p in doc.pages)


def test_page_numbers_go_only_at_the_page_edges() -> None:
    doc = clean(_doc("Intro line\n\nIslamabad\n73\nKarachi\n23\n\nClosing line\n\n14",
                     "iv\n\nSecond page body\n\nend", "Third page\n\nPage 3 of 9"))
    first = doc.pages[0].text
    assert "\n73\n" in first and "\n23\n" in first      # chart values in the body stay
    assert not first.rstrip().endswith("14")             # the page number at the foot goes
    assert not doc.pages[1].text.startswith("iv")
    assert "Page 3 of 9" not in doc.pages[2].text


def test_table_of_contents_runs_are_removed_but_short_number_lists_are_kept() -> None:
    toc = ("Contents\nRegistration of Courses 13\nAcademic Warning/Probation 18\n"
           "Summer Semester Rules 22\nPolicy on Generative AI Based Tools 34\n\nBody text.")
    stats_doc = clean(_doc(toc, "Islamabad 73\nKarachi 23\nLahore 50\nPeshawar 12", "x"))
    assert "Academic Warning/Probation 18" not in stats_doc.pages[0].text
    assert "Body text." in stats_doc.pages[0].text
    assert "Islamabad 73" in stats_doc.pages[1].text      # one-word labels: a table, not a TOC
    assert stats_doc.stats["toc_lines_removed"] == 4


def test_a_multi_word_fee_list_is_data_not_a_table_of_contents() -> None:
    fees = ("Charges\nLibrary fine per day 50\nHostel security deposit 500\n"
            "ID card replacement 200\nTranscript issuance fee 100\n\nEnd.")
    doc = clean(_doc(fees, "b", "c"))
    for line in ("Library fine per day 50", "Hostel security deposit 500",
                 "ID card replacement 200", "Transcript issuance fee 100"):
        assert line in doc.pages[0].text
    assert doc.stats["toc_lines_removed"] == 0


def test_grading_weights_on_an_ordinary_page_are_kept() -> None:
    page = ("Assessment\nThe final grade is made up as follows.\nQuizzes and Assignments 10\n"
            "Mid Term Exams 30\nProject Work 30\nFinal Exam 40\nThe weights may vary by course.")
    doc = clean(_doc(page, "b", "c"))
    assert "Mid Term Exams 30" in doc.pages[0].text and "Final Exam 40" in doc.pages[0].text
    assert doc.stats["toc_lines_removed"] == 0


def test_blank_lines_inside_a_kept_run_are_restored() -> None:
    doc = clean(_doc("Intro.\n\nLab Work 10\n\nStudents who miss a lab must repeat it.", "b", "c"))
    assert "Lab Work 10\n\nStudents who miss" in doc.pages[0].text


def test_a_sentence_that_starts_like_the_running_header_is_kept() -> None:
    header = "National University of Computer and Emerging Sciences"
    pages = [f"{header}\n\nClause {i} text.\n\nmore {i}\n\nend {i}" for i in range(1, 5)]
    pages[1] = f"{header}\nendeavours to inculcate integrity in its students.\n\nmore\n\nend"
    doc = clean(_doc(*pages))
    assert "National University of Computer and Emerging Sciences\nendeavours" in doc.pages[1].text
    assert header not in doc.pages[0].text                 # the real header still goes


def test_real_compounds_keep_their_hyphen_across_a_line_break() -> None:
    doc = clean(_doc("Students need self-\nmotivation and AI-\ngenerated work is non-\n"
                     "compliant, but regis-\ntration is one word.", "b", "c"))
    text = doc.pages[0].text
    assert "self-motivation" in text and "AI-generated" in text and "non-compliant" in text
    assert "registration" in text


def test_a_single_letter_at_the_page_edge_is_not_a_page_number() -> None:
    doc = clean(_doc("Grades\nA\nB\nc", "b", "c"))
    assert doc.pages[0].text.rstrip().endswith("c")


def test_dot_leader_toc_lines_count_even_without_a_contents_heading() -> None:
    toc = "Introduction ........ 1\nGrading ........ 11\nFees ........ 20\nIndex ........ 31\n"
    assert clean(_doc(toc + "\nReal body.", "b", "c")).pages[0].text.strip() == "Real body."


def test_duplicate_pages_are_dropped_keeping_the_first() -> None:
    doc = clean(_doc("Same text on two pages.", "Same   text on two pages.", "Different."))
    assert [p.page for p in doc.pages] == [1, 3]
    assert doc.stats["duplicate_pages_removed"] == 1


def test_characters_spacing_and_hyphenation_are_repaired() -> None:
    doc = clean(_doc("The city�s metro. The ﬁnal exam. Half � hour.\n"
                     "Spring S e m e s t e r starts in January.\n"
                     "Late regis-\ntration needs approval. Full-time stays.", "b", "c"))
    text = doc.pages[0].text
    assert "city's" in text and "final exam" in text and "Half  hour" not in text
    assert "Spring Semester starts" in text
    assert "registration needs approval" in text and "Full-time" in text


def test_structure_and_policy_content_are_never_stripped() -> None:
    text = ("## 5. Academic Warning\n\n5.4 A student whose CGPA falls below 2.00 is warned.\n"
            "a) Must meet the advisor.\n\n| Grade | Points |\n| A+ | 4.00 |\n\n"
            "*Footnote: hardship cases only.")
    doc = clean(_doc(text, "other page", "third page"))
    for kept in ("## 5. Academic Warning", "5.4 A student", "a) Must meet", "| A+ | 4.00 |",
                 "*Footnote"):
        assert kept in doc.pages[0].text


def test_metadata_and_page_numbers_survive_cleaning() -> None:
    original = Document("old.pdf", "2023-08", True, [Page("old.pdf", 7, "Text.", ocr=True)])
    doc = clean(original)
    assert (doc.source, doc.doc_version, doc.superseded) == ("old.pdf", "2023-08", True)
    assert doc.pages[0].page == 7 and doc.pages[0].ocr is True
