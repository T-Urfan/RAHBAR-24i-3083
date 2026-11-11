# Indexing and retrieval pipeline

The indexer (`indexer/pipeline/`) runs as a Kubernetes Job: extract → clean → chunk →
embed → store, one document at a time, then exits. It is the only writer to Chroma.
Every number below was measured on the real corpus ([CORPUS.md](CORPUS.md)) on
11 November 2026 unless it is marked **pending**.

## Extraction

`extract.py`: PDF, HTML, Markdown and text become *light Markdown* page by page:
`#` lines are headings, `| a | b |` lines are table rows, blank lines separate
paragraphs. Every page keeps its 1-based page number in the file.

What the real files required, in the order we found it:

| Problem in the corpus | What the code does |
| --- | --- |
| The 2026 handbooks store the **facing printed page's text outside each page's visible box**. `extract_text()` returns it, interleaved line by line ("1.9 A theory course … d) For pre-planned activities …"), so every clause would be indexed twice, on the wrong page. | Only objects wholly inside the page's CropBox are read (`within_bbox`). 29 of 58 handbook pages and 58 of 64 Student Handbook pages carry such text (`pages_with_offpage_text`). |
| **Multiple columns.** The prospectus prints four columns per page. | Empty vertical gutters are found from word positions (at least 7 pt wide, every column at least 18% of the text width) and each column is read top to bottom. Lines that cross a gutter (running headers, full-width titles) are read as their own band, in order. |
| **Tables.** pdfplumber's ruled-table finder also reports page frames and header boxes. | A table is kept only if it is smaller than 85% of the page, outside the top/bottom 10%, at least 2×2 and half filled. It is rendered as pipe rows at its position on the page, and its text is removed from the prose. |
| **Headings** carry no tags in a PDF. | A short line at least 1.5 pt larger than the page's body text (handbooks: 12 pt over 10 pt), or set in a heavier weight of the same size (prospectus: FiraSans-Regular over FiraSans-Light), and whose sentence does not run on into the next line. Two heading lines in a row are one wrapped heading. |
| **Scanned pages** (Aug 2023 rules, Entrepreneurship Policy). | pdfium counts each page's text-layer characters first; under 25 means no text layer. Such a page never reaches pdfplumber (the 2023 rules are about 3,000 vector paths per page: pdfplumber took 118 s on that file, pdfium 2 s). It is rendered by pdfium at 300 dpi (capped at 40 Mpx) and read by Tesseract (`--psm 3`, 180 s timeout per page). Page metadata: `ocr=true`. |
| **HTML pages** carry menus, a wrong `<title>` ("Accreditation"), nested tables, `colspan`. | `<head>`, `nav`, `header`, `footer`, forms and scripts are dropped; `h1`–`h6` become `#` headings; tables become pipe rows (a `colspan` cell is padded, a nested table's text stays in its cell, omitted `</td>`/`</tr>` are closed). |

**Which documents needed OCR:** Undergraduate Academic Rules Aug 2023 (24 of 26 pages)
and the Faculty Entrepreneurship Policy (8 of 8). Cover pages of the 2026 handbooks
(9 pages) have no text layer either.

**One before-and-after example of a mangled table** (Prospectus 2026, page 1, grade
tables beside the attendance rule). Before, with `page.extract_text()`:

```text
Letter grades for MS & MBA pr ograms:
1. Attendance Requirement 4. Semester Course Load
Gr ade Po ints Grade Points
Students are required to maintain Each course is assigned credit
A+ 4.00 B- 2.67
100% attendance in lectures, labs, hours. For theory courses 1 credit
```

After (column-aware extraction): the attendance rule reads straight through ("…
Minimum attendance requirement for Undergraduate degree programs are 80%. Minimum
attendance requirement for Graduate and Post Graduate degree programs are 75%."), and
each programme's grade table is separate:

```text
Letter grades for BS & BBA programs:
Gr ade Po ints Grade Points
A+ 4.00 C+ 2.33
...
B- 2.67 F 0.00

Letter grades for MS & MBA pr ograms:
Gr ade Po ints Grade Points
A+ 4.00 B- 2.67
...
```

A ruled table comes out as cells (Handbook 2026, page 12). Before: `A+ 4.00 C+ 2.33` with
the column boundaries lost. After: `| A+ | 4.00 | C+ | 2.33 |` under
`| Grade | Points | Grade | Points |`.

**What we could not recover:**
- **Split glyphs inside words** ("pr ograms", "Gr ade Po ints"): the PDF positions
  letters individually, and joining them back would also join real words.
- **Lost glyphs:** some characters come out as U+FFFD. Between two letters we restore an
  apostrophe; elsewhere the original character is unknown.
- **The prospectus grade tables** are not ruled, so they stay space-separated lines, not
  cells. Each programme's table is still kept separate.
- **The 2023 rules' wording** depends on OCR quality. This is **pending**: Tesseract is not
  installed on the development machine yet.

## Cleaning

`clean.py` applies these rules, in this order:

1. Unicode NFKC. U+FFFD between letters becomes an apostrophe; elsewhere it becomes a space.
2. Letter-spaced words are joined ("S e m e s t e r" → "Semester").
3. A word hyphenated across a line break is joined, unless it is a compound: a known
   prefix ("self-", "non-") or a capitalised first part ("AI-generated") keeps its hyphen.
4. **Running headers and footers** are removed. These are lines in a page's first or last
   three lines that appear at the edge of at least 40% of the document's pages, with page
   numbers ignored (odd and even pages put the number on opposite sides). A line is not
   removed if its sentence continues on the next line: Student Handbook p. 9 opens with
   "National University of Computer and Emerging Sciences / endeavours to …".
5. A bare **page number** is removed only as the first or last line of a page. Elsewhere,
   a lone number is a chart value: 183 of them in Annual Report 2019-20 were wrongly
   removed before this rule was narrowed.
6. **Table-of-contents runs** are removed: four or more lines ending in page numbers that
   never decrease, on a contents page (a "Contents" line, or at least 8 TOC-shaped lines
   making up half the page) or written with dot leaders. 53 lines go from the handbook,
   13 from the Student Handbook, and 31 from the annual reports.
7. A page identical to an earlier page of the same document is dropped (Student
   Handbook pp. 12/13 and 44/45).

**What we deliberately did not strip:** clause numbers ("4.18"). A student asks "what
does clause 1.21 say?", and the clause number is also how the recall evaluation matches
an answer to a question (the `clauses` metadata). Headings, table rows, `a)` list markers
and footnotes also stay. A fee list ("Library fine per day 50 …") or a marks breakdown
("Mid Term Exams 30") looks like a TOC but is data, so it is kept.

## Chunking

`chunk.py`, **structure** strategy (the default). Sizes are in **words**
(whitespace-separated tokens), not model tokens. The indexer needs no tokenizer, and one
English word is about 1.3 nomic tokens, so 400 words ≈ 520 tokens.

1. A heading starts a new section. **A chunk never crosses a section**, so every chunk
   has one heading to cite.
2. Within a section, the units are paragraphs, and **every numbered clause starts its own
   paragraph**, so a clause is never separated from its number.
3. Units are packed in order up to `CHUNK_SIZE` words.
4. A longer unit is split at **sentence** boundaries; only a single sentence longer than
   `CHUNK_SIZE` is cut into overlapping word windows. A long table is split by rows,
   **repeating its header row** in every part.
5. **Overlap:** the next chunk in the same section starts with the previous chunk's last
   whole sentences, up to `CHUNK_OVERLAP` words. Nothing overlaps across sections.

Chunk ids are `<file-slug>-<index>-<sha1(source|version|section|text)[:10]>`. They are
readable, identical on every run with the same input and settings, and unique within a
run (duplicates are rejected before writing).

**Shipped:** `CHUNK_SIZE=400`, `CHUNK_OVERLAP=50` words, which gives 469 chunks and a
**storage amplification of 1.024** (58,832 stored words / 57,427 source words). That is
far below the lecture's 150/120 = 1.25 because most sections here are shorter than 400
words, and overlap only applies inside a section: only a few dozen neighbouring chunk
pairs share a section.

The three configurations measured (text-layer documents; the scanned ones are added
once OCR runs):

| Size (words) | Overlap (words) | recall@5 | Chunks stored | Amplification |
| --- | --- | --- | --- | --- |
| 200 | 30 | **pending** | 566 | 1.051 |
| 400 | 50 | **pending** | 469 | 1.024 |
| 600 | 80 | **pending** | 449 | 1.018 |

recall@5 needs real `nomic-embed-text` embeddings and the golden set, so it is filled in
by phase P8. **The shipped value is provisional until then.** The rubric scores a number
with no measurement behind it as zero, and we will not present one.

`CHUNK_STRATEGY=fixed` gives plain word windows that ignore structure. It exists only to
break stage B on purpose in [FAILURE-ANALYSIS.md](FAILURE-ANALYSIS.md).

## Embedding

- **Model:** `nomic-embed-text` via Ollama, **768 dimensions**.
- **The pin:** `EMBED_MODEL` in the ConfigMap, read by both the indexer and the API.
  Every index records `embed_model` and `dimension` in its collection metadata; the API's
  `/readyz` refuses to report ready when they differ from its own `EMBED_MODEL`
  (`api/app/services/pin.py`).
- **Task prefixes:** each chunk is embedded as `search_document: <section>\n<text>`. The
  heading gives a short clause its topic. Questions must be embedded as
  `search_query: <question>` with the same `normalise()` function (NFKC, whitespace
  collapsed). Both prefixes are constants in `indexer/pipeline/embed.py`, and the API's
  query side (P6) must use them unchanged.
- **Batching:** `EMBED_BATCH_SIZE=16` texts per `POST /api/embed`, sent with
  `truncate: false`, so an over-long chunk is an error rather than silently cut. Each
  batch is written to Chroma before the next is embedded.
- **Failures:** timeouts, connection errors and HTTP 408/429/5xx are retried
  (`EMBED_RETRIES=3`, backoff 1, 2, 4 s). Any other 4xx fails at once with a fix (for
  example "Run: ollama pull nomic-embed-text"). Vectors are checked for count, dimension,
  NaN or infinity, and all-zero values.

## Storing

`store.py`, the only code that writes to Chroma:

1. Each run writes a **new collection** `rahbar-v<UTC timestamp to the µs>-<6 hex>`,
   using cosine distance, and checks for duplicate chunk ids before writing.
2. It verifies that the stored row count equals what was written, then records the run
   statistics (`chunk_size`, `chunk_overlap`, `amplification`, `documents`,
   `failed_documents`, `ocr_pages`, `tables`).
3. Only then is the pointer collection `rahbar-active` repointed. The API reads whatever
   the pointer names. A run that fails before this point never changes what is served,
   and its unfinished collection is deleted. After the pointer moves, nothing is ever
   deleted on failure: the reply may have been lost after the server applied the change.
4. The previous index is kept, so `python -m pipeline.run --rollback` switches back.
   Older versions of *this* collection name are deleted. Newer ones are not, because they
   may be another run's build in progress.

A document that cannot be extracted, or yields no chunks, blocks activation unless
`ALLOW_PARTIAL_INDEX=true`. The old index keeps serving, and the log names the file.

**Chunk metadata schema** (Chroma values are str, int, float or bool, never null):

| Field | Type | Example | Purpose |
| --- | --- | --- | --- |
| id (record id) | str | `academic-rules-handbook-jun-2026-0019-…` | stable chunk id |
| `source` | str | `academic-rules-handbook-jun-2026.pdf` | citation, filtering |
| `section` | str | `Academic Warning/Probation` | citation |
| `page` | int | `24` | citation (first page of the new content) |
| `page_end` | int | `24` | last page the chunk reaches |
| `doc_version` | str | `2026-06` | version-aware answers |
| `superseded` | bool | `false` | default retrieval filter (failure mode E) |
| `clauses` | str | `5.4,5.5,5.6,5.7` | exact-term matching; omitted when none |
| `ocr` | bool | `false` | text came from OCR |
| `chunk_index`, `words` | int | `19`, `126` | diagnostics |

**Chunk count:** 469 at the shipped settings, before the two scanned documents are OCR'd.

## Memory

Measured on Windows over the whole text-layer corpus (extract, clean and chunk; no OCR,
no embedding): a **peak working set of 119 MB**. The run streams, so memory holds one
document's text and one embedding batch, never the corpus:

- pages are freed as soon as they are read;
- vectors are written 16 at a time, about 50 KB for 768-dimension floats;
- one OCR page renders to at most 40 Mpx greyscale (an A4 page at 300 dpi is about
  9 MB), released after Tesseract reads it. Tesseract itself runs as a separate process.

**Pending:** the Job's real peak with OCR, from `kubectl top pod` in phase P11; that
number sets its memory limit. The indexer also logs `peak_rss_mb` per document on Linux.

## Semantic cache

**Pending (phase P7):** thresholds tried, the false hit that ruled one out, the one shipped.

## Evaluation method

**Pending (phase P8).** The contract fixed now: a golden passage is
`{source, section}`. It matches a retrieved chunk with the same `source` whose `section`
starts with the golden section (after case and trailing-dot normalisation), or whose
`clauses` list contains it. So the golden `section: "5.4"` from the example set matches
the chunk under "Academic Warning/Probation" with `clauses: 5.4,…`. The fixture golden
set's sections are produced exactly by the chunker; `test_fixture_sections_match_the_golden_set_exactly`
guards this.
