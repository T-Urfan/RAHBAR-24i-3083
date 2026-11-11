"""Cite only what you retrieved (section 3.4).

Every citation the model returns is matched against the passages it was actually
given for this request. A match is the same source and the same section, compared
without case, extra spaces or trailing punctuation (a 3B model copies "5.4 Academic
Warning." as "5.4 academic warning"). A matched citation is rewritten to the
passage's exact fields, so what the API returns always names a retrieved chunk. A
citation that matches nothing is fabricated: it is dropped and logged, never shown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..schemas import Citation, Passage


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().strip(".:;,").casefold()


@dataclass(frozen=True)
class CitationCheck:
    valid: list[Citation]
    invalid: list[Citation]


def check_citations(citations: list[Citation], passages: list[Passage]) -> CitationCheck:
    by_key: dict[tuple[str, str], list[Passage]] = {}
    for p in passages:
        by_key.setdefault((_norm(p.source), _norm(p.section)), []).append(p)
    unique: dict[tuple[str, str, int | None], Citation] = {}
    invalid: list[Citation] = []
    for c in citations:
        matches = by_key.get((_norm(c.source), _norm(c.section)))
        if not matches:
            invalid.append(c)
            continue
        # keep the cited page if one of the matching passages is on it
        chosen = next((p for p in matches if p.page == c.page), matches[0])
        unique.setdefault((chosen.source, chosen.section, chosen.page),
                          Citation(source=chosen.source, section=chosen.section,
                                   page=chosen.page))
    return CitationCheck(list(unique.values()), invalid)
