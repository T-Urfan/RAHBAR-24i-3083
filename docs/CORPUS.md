# Corpus

Source: the FAST-NUCES public website (nu.edu.pk), public pages only · Documents: 9
(7 PDF, 2 HTML) · Pages: 279 PDF pages plus 2 web pages · Words after cleaning: 64,079,
of which 41 pages were read by OCR · Chunks: 501 at the shipped 400/50 words

The files are not committed. `corpus/sources.yaml` lists them, `corpus/fetch.py` downloads
them one at a time with a 2 s pause, and [`corpus/MANIFEST.md`](../corpus/MANIFEST.md) records
each URL, size and SHA-256. `python corpus/fetch.py --verify` re-checks the bytes. Every
document below was downloaded and checked on 11 November 2026.

## The seven required properties

| # | Required property | Document(s) that provide it | Evidence or notes |
| --- | --- | --- | --- |
| 1 | At least 6 documents, 40 pages | All 9 documents | 279 PDF pages: Academic Rules Handbook 2026 (58), Student Handbook 2026 (64), Annual Report 2023-24 (85), Annual Report 2019-20 (33), Undergraduate Academic Rules Aug 2023 (26), Entrepreneurship Policy (8), Prospectus rules (5); plus the Fee Structure and Grading web pages. |
| 2 | Two versions of one policy | Undergraduate Academic Rules, **Aug 2023** (`doc_version 2023-08`, `superseded: true`) and Academic Rules Handbook, **Jun 2026** (`2026-06`, `superseded: false`) | The 2026 handbook replaces the 2023 rules. `superseded` and `doc_version` come from `corpus/sources.yaml`, travel through `corpus/raw/_index.json`, and are written on every chunk. After OCR the two versions were diffed clause by clause. Real changes: **4.25** withdrawal deadline ("last day of classes" in 2023, "last day of the makeup week" in 2026); **1.6** semester length ("15/16 weeks of teaching" vs "16 weeks of teaching, one makeup week"); **4.11** FYP-I registration (2023 also requires a zero warning count); **5.8** repeating a failed course. Golden questions q14 to q16 use 4.25 and 1.6. A softer version effect: the Prospectus 2026 states the same rules with different clause numbers (attendance is clause **1** there, **1.21** in the handbook). |
| 3 | Scanned, image-only PDF | Faculty Entrepreneurship and Technical Start-ups Policy (8 of 8 pages without a text layer) **and** the Aug 2023 rules (24 of 26 pages) | `pdffonts` is not available on Windows, so the proof is pdfium's text layer: 0 extractable characters on those pages (`pages_without_text_layer` in the indexer log). The 2023 rules are text converted to vector outlines (about 3,000 paths and 0 characters per page), not a raster image; the effect is the same: no text without OCR. |
| 4 | A table with real information | Handbook 2026 pp. 11–12 (letter grades and grade points), Student Handbook p. 33 (library loan limits and fines), Annual Report 2019-20 pp. 24–25 (publications by campus and department), Fee Structure page (tuition per credit hour, miscellaneous fees, **refund timeline**), Grading page (grade points and interpretations for each programme level) | Extracted as `\| a \| b \|` rows; see "Extraction" in [PIPELINE.md](PIPELINE.md) for the before and after. |
| 5 | Long definitions | "Policy on Generative AI Based Tools": Academic Rules Handbook 2026 (pp. 40–55, twelve numbered parts) and Student Handbook §XII (pp. 48–61) | Long numbered sub-sections defining acceptable and unacceptable use. The Student Handbook section alone produces chunks of 370+ words at `CHUNK_SIZE=400`, i.e. it spans several chunks and a fixed-size chunker cuts definitions mid-sentence. |
| 6 | A deliberately absent topic | **Hostel rules**: allotment, fees, application, conduct | The prospectus's *Transport and Hostel Facilities* PDF is left out on purpose. The corpus still *mentions* hostels (Annual Reports: campuses have hostels; Student Handbook p. 45: "In-Campus Hostel Facility is exclusively provided at Chiniot-Faisalabad and Peshawar Campuses"), which makes this a realistic near-miss: retrieval will return hostel-flavoured passages, but none says how to apply, what it costs or what the rules are. Must-abstain questions ask exactly that. A student would plausibly ask: hostel places are limited and the rules are not in any handbook we index. |
| 7 | Exact-term content | Clause numbers, thresholds and fees across the corpus | **Clause 5.4**: minimum CGPA to avoid academic warning is **2.00** (5.5: warning when CGPA drops below 2.00; 5.6: three consecutive semesters closes admission). **Clause 1.21**: 100% attendance required, at most 20% condonable. Prospectus clause 1: minimum attendance **80%** undergraduate, **75%** graduate. Tuition **Rs. 12,000 per credit hour** (2026-27), admission fee **Rs 30,000**, security deposit **Rs 20,000**; refunds **100% / 80% / 60% / 50%** up to day 10 / 15 / 20 / 30, none from day 31. |

## The documents

| Document | Version | Superseded | Pages | Kind | Why it is here |
| --- | --- | --- | ---: | --- | --- |
| Undergraduate Academic Rules (Aug 2023) | 2023-08 | yes | 26 | scanned (24 pages) | The old version of the rules: failure mode E, and OCR |
| Academic Rules Handbook (Jun 2026) | 2026-06 | no | 58 | text, two pages per sheet | The current rules: numbered clauses, grade tables |
| Prospectus 2026: Academic Rules and Regulations | 2026 | no | 5 | text, four columns | The same rules, different clause numbers |
| Student Handbook (Jun 2026) | 2026-06 | no | 64 | text, two pages per sheet | Long definitions (GenAI policy), library tables |
| Annual Report 2019-20 | 2019-20 | no | 33 | text | Tables; an older edition of a yearly report |
| Annual Report 2023-24 | 2023-24 | no | 85 | text | The newer edition: same tables, different numbers |
| Fee Structure page | 2026-27 | no | 1 | HTML | Fees and the refund timeline as HTML tables |
| Grading page | 2026 | no | 1 | HTML | Grade points per programme level |
| Faculty Entrepreneurship and Technical Start-ups Policy | 2022-05 | no | 8 | scanned (all pages) | The fully image-only document |

The two annual reports are both marked `superseded: false`: each is the correct record of its
own year, not a repealed rule. `doc_version` still lets a question about one year be answered
from that year's report.

## Traps found in the documents themselves

These are properties of the source files that the pipeline has to survive. Each one is
handled in the extraction stage and described in [PIPELINE.md](PIPELINE.md).

- **Hidden off-page text.** The two 2026 handbooks store the facing printed page's text
  outside every PDF page's visible box. `extract_text()` returns it, interleaved line by line
  with the real page, so every clause would be indexed twice under the wrong page number.
- **Multiple columns.** The prospectus prints four columns per page, with grade tables for
  each programme level side by side.
- **Running headers and footers** on every page, with the page number on alternating sides.
- **Tables of contents** (Handbook pp. 4–6), and **duplicated pages** (Student Handbook pp.
  12/13 and 44/45; Annual Report 2023-24 pp. 84/85).
- **Lost glyphs**: some characters come out as U+FFFD ("city�s" for "city's").
- **Wrong page metadata**: the Fee Structure page's HTML `<title>` is "Accreditation".

- **Two current documents disagree.** The Prospectus 2026 says a withdrawal request must
  reach the HoD "before the last day of classes"; the Academic Rules Handbook 2026 (4.25)
  says the deadline is "the last day of the makeup week". Neither is superseded, so
  metadata cannot choose between them. Golden question q14 lists both as relevant and
  asserts no expected string; the system answered from the handbook and cited it.

## What OCR recovered

Tesseract 5.4 (300 dpi, `--psm 3`) read all 41 pages without a text layer: 24 of the 2023
rules, all 8 of the Entrepreneurship Policy, and 9 cover pages of the 2026 handbooks. The
2023 rules gave 4,662 words and 20 chunks, with clause numbers intact (5.4 "Minimum CGPA
required to avoid academic warning 1s 2.00": the only damage is "is" read as "1s").
The grade-point table on p. 9 came out as fragments ("Bo (3.00 [D100 |"): a ruled table
in a scan is not recovered as a table. That loss is documented in PIPELINE.md.
