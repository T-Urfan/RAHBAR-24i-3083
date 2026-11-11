# ADR 0002: Structure-aware chunking in words, 400 with 50 overlap

## Status
Accepted · 11 November 2026

## Context
The corpus is rule books: numbered clauses ("5.4 Minimum CGPA required to avoid academic
warning is 2.00"), headings above them, fee tables, and a long generative-AI policy with
numbered sub-sections. A student asks about a clause, and the answer must cite a section.
A fixed-size window cuts a clause from its number, a table row from its header, and a
definition in half (failure mode B).

## Decision
`indexer/pipeline/chunk.py`, strategy `structure`:

- A heading starts a section, and **a chunk never crosses a section**, so every chunk has
  exactly one heading to cite.
- Inside a section the unit is the paragraph, and every numbered clause starts its own
  paragraph. Units are packed up to `CHUNK_SIZE`; a longer unit is split at sentences, and
  only a single over-long sentence is cut into overlapping word windows. A long table is
  split by rows with its header repeated.
- **Overlap** is whole sentences from the end of the previous chunk of the same section, up
  to `CHUNK_OVERLAP` words. Nothing overlaps across sections.
- The unit is **words**, not tokens: the indexer needs no tokenizer, and one English word is
  about 1.3 nomic tokens, so 400 words is about 520 tokens, inside nomic-embed-text's window.
- Each chunk records the clause numbers it contains (`clauses` metadata), which the
  evaluation and the citation check use.

Shipped: **CHUNK_SIZE=400, CHUNK_OVERLAP=50**, chosen from three measured settings
(docs/PIPELINE.md, "Chunking").

## Options considered
- **Fixed windows (the lab's chunker).** Kept as `CHUNK_STRATEGY=fixed` only to break
  stage B on purpose in FAILURE-ANALYSIS.md.
- **Token-based sizes.** More precise, but needs the model's tokenizer in the indexer image
  for no measured gain at these sizes.
- **Smaller (200/30) or larger (600/80) chunks.** Measured on the golden set; the numbers
  and the reason for 400/50 are in PIPELINE.md, "Chunking".

## Consequences
- Easier: citations are always a real heading; clause-number questions can be matched
  exactly.
- Harder: section detection depends on extraction finding headings, so a PDF with no
  visible heading style becomes one long section split by sentences.
- Watch: storage amplification is only 1.028, because most sections are shorter than 400
  words; a corpus of long sections would overlap more.
