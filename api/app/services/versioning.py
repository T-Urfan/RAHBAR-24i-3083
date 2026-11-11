"""Which policy version does a question ask about? (section 3.2, step 4; failure mode E)

By default retrieval excludes chunks whose metadata says ``superseded: true``, so a
repealed clause is never quoted as current. The filter is lifted only when the user
explicitly asks about an older version: "what did the 2023 rules say", "the old
policy", "before the revision". Then both versions are retrieved and each passage is
labelled with its doc_version and superseded flag in the prompt, so the model can say
which rule is which.

A keyword rule, not a model: it is deterministic, testable, and when it is wrong it is
wrong in the safe direction (it keeps the filter on).
"""

from __future__ import annotations

import re

# The current policy year in this corpus is 2026; any earlier year names an old version.
CURRENT_YEAR = 2026

# Deliberately narrow: "prior approval" or "change my section" are ordinary questions
# about the current rules and must keep the filter on.
_OLDER_WORDS = re.compile(
    r"\b(old|older|previous|previously|earlier (version|rules?|policy|handbook)|former|"
    r"formerly|superseded|outdated|repealed|obsolete|archived|used to|no longer|"
    r"before the (new|current|revised|latest)|what (has )?changed|has (it )?changed|"
    r"compared? (to|with) the (old|previous|earlier))\b",
    re.I)
_YEAR = re.compile(r"\b(19|20)(\d{2})\b")


def wants_older_version(question: str) -> bool:
    """True when the question explicitly refers to an older or superseded version."""
    if _OLDER_WORDS.search(question):
        return True
    return any(int(m.group(0)) < CURRENT_YEAR for m in _YEAR.finditer(question))
