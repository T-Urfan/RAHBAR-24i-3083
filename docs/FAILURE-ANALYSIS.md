# Failure analysis

Five of the six stages (A, B, C, E, F) were broken on purpose, one at a time, on the real corpus
(11 November 2026), then restored. Each block says how we would have found it without
knowing what we broke: which number or log line gives it away. Retrieval numbers come
from the golden set (`eval/golden.yaml`, 21 questions) with the API's own retriever; the
shipped system scores recall@5 **0.971** (`eval/results.json`).

As the RAG lecture says, only F is the model's fault; A to E are ours, so we check them
first, in order.

## Stage A · Extraction: index the scanned PDF without OCR

- **What we broke:** `OCR_ENABLED=false`, everything else shipped (400/50), into a separate
  collection. `ALLOW_PARTIAL_INDEX=true`, otherwise the indexer refuses to activate.
- **Symptom:** the Aug 2023 rules, 24 of 26 pages without a text layer, gave **2 chunks**
  instead of 20; the Entrepreneurship Policy gave none ("no text to index"). Retrieval:
  recall@5 0.971 → **0.912**; version-sensitive questions 1.00 → **0.67**. q16 ("Under the old
  2023 academic rules, what was the deadline to withdraw from a course?") retrieved only
  2026 passages (recall 0.0): the system would answer the 2023 question with the 2026 rule,
  and nothing errors. Questions about the 2026 rules are unaffected, so a spot check passes.
- **Diagnosis, as if we had not caused it:** the indexer's per-document log line:
  `"source": "undergraduate-academic-rules-aug-2023.pdf", "chunks": 2,
  "pages_without_text_layer": 24, "ocr_pages": 0`, and the summary's `"failed_documents": 1`.
  A document with 26 pages and 2 chunks is the giveaway: compare `chunks` with `pages`.
  In the API log, the `answered` line for q16 shows no chunk id from that source.
- **Remedy:** `OCR_ENABLED=true` (the default; Tesseract is in the indexer image). The
  indexer refuses to activate an index with a failed document unless told otherwise, so
  this break needed an explicit override to reach production.

## Stage B · Chunking: fixed-size 100-word chunks, no overlap

- **What we broke:** `CHUNK_STRATEGY=fixed`, `CHUNK_SIZE=100`, `CHUNK_OVERLAP=0`.
- **Symptom:** 653 chunks instead of 501. **60 of the 88** Academic Rules Handbook chunks
  start mid-sentence (0 of 59 when structure-aware). Clause 5.4 lands in a chunk that begins
  "...urse will be made with the approval of Dean of the Faculty. 5.4 Minimum CGPA required
  to avoid academic warning is 2.00 ... 5.5 If at the end of a", so the next clause is cut
  in half, and the chunk's section is "5. Academic Assessment", the heading of the window's
  first line, not the clause's own heading "Academic Warning/Probation". recall@3 fell
  0.971 → 0.941 (recall@5 stayed 0.971). The definitional question q08 (unacceptable uses of
  generative AI) lost half its relevant passages. The end-to-end effect on answers is in
  the table below.
- **Diagnosis:** recall@5 alone hides this, because a section-level match still finds the
  section. What shows it: retrieval scores and `recall@3` falling while recall@5 holds, the
  chunk count jumping by 30%, and, reading the `answered` log line's chunk ids, retrieved
  text that starts lower-case. A citation whose section does not contain the quoted clause
  is the user-visible symptom.
- **Remedy:** `CHUNK_STRATEGY=structure` (shipped): a chunk never crosses a heading and every
  clause starts its own paragraph (ADR 0002).

## Stage C · Retrieval: top_k 1, and EMBED_MODEL changed on the API only

- **What we broke (1):** `TOP_K=1`.
- **Symptom:** recall@5 0.971 → **0.588**. Version-sensitive questions 1.00 → **0.17**: every
  question with two relevant passages (the handbook and the prospectus) loses one, and q16's
  2023 passage ranks second behind the 2026 one. Answers look fine and cite correctly; they
  are just built on half the evidence.
- **Diagnosis:** the `answered` log line carries `"top_k": 1` and a single chunk id; the
  eval's recall falls far below the baseline and the CI gate fails (more than 5 points).
- **What we broke (2):** `EMBED_MODEL=mxbai-embed-large` on the API, index built with
  `nomic-embed-text`.
- **Symptom:** `/readyz` 503, the pod alive but not ready
  (`docs/evidence/pin-mismatch-readyz-503.txt`). Without the guardrail it would have queried a
  nomic index with another model's vectors: no error, nonsense retrieval.
- **Diagnosis:** the readiness log line `"failed": ["embed_model_mismatch"]` names both models.
- **Remedy:** `TOP_K=5` from the ConfigMap; re-index with the new model, or set it back.

## Stage E · Source: remove the superseded filter

- **What we broke:** retrieval with `include_superseded=True` for every question (the filter
  `where={"superseded": false}` removed).
- **Symptom:** for "What is the deadline to withdraw from a course?" the 2023 rules' chunk
  (4.25: "last day of classes") is retrieved third (similarity 0.831) beside the 2026 rule
  ("last day of the makeup week"). The model still answered with the 2026 rule, because the
  prompt labels each passage `version: 2023-08, SUPERSEDED`. With the labels also removed,
  **one of three runs answered "Last day of classes"** while citing the 2026 handbook, which
  says the opposite. (The current prospectus also says "before the last day of classes",
  a genuine conflict between two current documents: CORPUS.md.)
- **Diagnosis:** the `answered` log line's chunk ids include a `...-aug-2023-...` chunk for a
  question that never mentioned 2023, and `RetrievedRef.superseded` is `true` in the response.
  A metric that would catch it: share of retrieved chunks with `superseded: true` on questions
  where `include_superseded` was false must be 0.
- **Remedy:** the metadata filter (shipped), lifted only when the question asks about an older
  version (`api/app/services/versioning.py`). The prompt label is defence in depth, not the fix:
  a prompt is a request, a filter is a guarantee.

## Stage F · Generation: temperature 1.2

- **What we broke:** `TEMPERATURE=1.2` (shipped 0.1).
- **Symptom:** retrieval is unchanged (recall@5 0.971), and the JSON stays valid with no
  retries or fallbacks, because Ollama's `format=` constrains decoding to the schema
  whatever the temperature. The words drift: expected facts found fell from 14 to 13 of 15,
  q03 dropped the "ineligible for the final exam" half of the rule, and q09 answered "A
  minimum of TEN (10) students" at confidence `high`, so the confidence gate let it through.
  The same question gives different answers on two runs.
- **Diagnosis:** retrieval is identical to the baseline (same chunk ids and scores in the
  log), so A to E are ruled out; what changes is `rahbar_generation_retries_total`,
  `rahbar_fallbacks_total` and answers that differ between two runs of the same question.
- **Remedy:** temperature back to 0.1. Temperature 0 does not fix hallucination: when the
  knowledge is absent, a deterministic wrong answer is still wrong, which is why abstention
  is decided by the relevance floor and the confidence gate, not by temperature.

## End-to-end, with generation

| Run | recall@5 | Answered (of 17) | Expected fact found | Must-abstain declined | Citation validity | Fallbacks |
| --- | --- | --- | --- | --- | --- | --- |
| shipped (400/50, structure, T 0.1) | 0.971 | 16 (q09 declined at low confidence) | 14 of 15 | 4 of 4 | 100% | 0 |
| B: fixed 100/0 chunks | 0.971 (recall@3 0.941) | 16 | 13 of 15 | 4 of 4 | 100% | 1 (q14) |
| F: temperature 1.2 | 0.971 | 17 | 13 of 15 | 4 of 4 | 100% | 0 |

Under stage B, q14 fell back: the model cited "4.25" twice and the citation check could not
map it to a retrieved fixed-window chunk (log: `"rejected_citations":
["academic-rules-handbook-jun-2026.pdf / 4.25"]`, `"fallback": "citations"`). q15 answered
"16 + 1 + 2 = 19", adding the exam weeks to the teaching and makeup weeks; all three numbers
are in clause 1.6, so that one we cannot blame on the chunk boundary alone.

The abstention probes (10 hostel and other absent topics) were declined 10 of 10 by the
shipped system, and 8 of 10 at 200/30 chunks (PIPELINE.md, "Chunking").
