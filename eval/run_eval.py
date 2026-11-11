#!/usr/bin/env python3
"""Score a running RAHBAR against a golden set, and gate CI on the result (section 5).

    python eval/run_eval.py --api http://<ingress>                 # your corpus
    python eval/run_eval.py --api ... --golden eval/fixtures/golden.fixture.yaml
    python eval/run_eval.py --api ... --update-baseline            # after a deliberate improvement

Exit code 0 when every gate passes, 1 when any fails. CI relies on that exit code.

Matching rules (docs/PIPELINE.md, "Evaluation method"): a golden passage matches a
retrieved chunk by source plus section prefix, or by clause number; a citation is valid
when a retrieved chunk has the same source and section (and page, when both give one).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
import yaml

HERE = Path(__file__).resolve().parent

# The gate from section 5.4. Change these only with a reason written in docs/PIPELINE.md.
MAX_RECALL5_DROP = 0.05        # recall@5 may fall at most 5 points below the baseline
MIN_CITATION_VALIDITY = 0.95   # at least 95% of citations must match retrieved passages
REQUIRED_ABSTENTION = 1.0      # every must-abstain question must be declined


def load_golden(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    questions = data.get("questions") or []
    if not questions:
        sys.exit(f"no questions in {path}")
    return questions


def ask(client: httpx.Client, api: str, question: str) -> dict:
    """POST one question and return the AskResponse as a dict (see api/app/schemas.py)."""
    response = client.post(f"{api.rstrip('/')}/api/ask", json={"question": question})
    response.raise_for_status()
    return response.json()


_QUOTES = str.maketrans({"’": "'", "‘": "'", "�": "'", "`": "'"})


def _norm(text: str) -> str:
    """Sections compare without case, spacing, curly quotes or a trailing dot."""
    return " ".join(str(text).translate(_QUOTES).split()).rstrip(".:").casefold()


def passage_matches(golden: dict, ref: dict) -> bool:
    """A retrieved chunk matches a golden passage when the source is the same and either
    its section starts with the golden section ("Tuition Fee" matches "Tuition Fee (w.e.f.
    Fall 2026 semester)", and a long section split over several chunks matches each of
    them), or the golden clause number is one of the chunk's clauses."""
    if _norm(ref.get("source", "")) != _norm(golden.get("source", "")):
        return False
    section = _norm(golden.get("section", ""))
    if section and _norm(ref.get("section", "")).startswith(section):
        return True
    clause = str(golden.get("clause", "")).strip()
    return bool(clause) and clause in [str(c).strip() for c in ref.get("clauses") or []]


def recall_at_k(relevant: list[dict], retrieved: list[dict], k: int) -> float | None:
    """Fraction of the relevant (source, section) pairs found in the top k retrieved.
    Return None when the question has no relevant passages (must-abstain questions)."""
    if not relevant:
        return None
    top = retrieved[:k]
    found = sum(1 for g in relevant if any(passage_matches(g, r) for r in top))
    return found / len(relevant)


def _citation_matches(citation: dict, ref: dict) -> bool:
    if _norm(citation.get("source", "")) != _norm(ref.get("source", "")):
        return False
    if _norm(citation.get("section", "")) != _norm(ref.get("section", "")):
        return False
    page = citation.get("page")
    return page is None or ref.get("page") is None or page == ref.get("page")


def citation_validity(response: dict) -> float | None:
    """Fraction of the response's citations that match a passage in response['retrieved'].
    Return None when there are no citations."""
    citations = response.get("citations") or []
    if not citations:
        return None
    retrieved = response.get("retrieved") or []
    valid = sum(1 for c in citations if any(_citation_matches(c, r) for r in retrieved))
    return valid / len(citations)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def summarise(rows: list[dict]) -> dict:
    """Overall recall@3, recall@5, citation validity and abstention correctness, plus
    recall@5 broken down by question type. This dict becomes results.json."""
    per_question = []
    r3s: list[float] = []
    r5s: list[float] = []
    by_type: dict[str, list[float]] = {}
    cited = valid = 0
    abstain_total = abstain_ok = 0
    answerable = answered = 0
    expect_total = expect_hit = 0
    for row in rows:
        resp = row["response"]
        retrieved = resp.get("retrieved") or []
        r3 = recall_at_k(row["relevant"], retrieved, 3)
        r5 = recall_at_k(row["relevant"], retrieved, 5)
        if r3 is not None and r5 is not None:
            r3s.append(r3)
            r5s.append(r5)
            by_type.setdefault(row["type"], []).append(r5)
        citations = resp.get("citations") or []
        cv = citation_validity(resp)
        if cv is not None:
            cited += len(citations)
            valid += round(cv * len(citations))
        answered_from_context = bool(resp.get("answered_from_context"))
        if row["must_abstain"]:
            abstain_total += 1
            abstain_ok += not answered_from_context
        else:
            answerable += 1
            answered += answered_from_context
        hit = None
        if row.get("expect") and not row["must_abstain"]:
            expect_total += 1
            hit = str(row["expect"]).casefold() in str(resp.get("answer", "")).casefold()
            expect_hit += hit
        per_question.append({
            "id": row["id"], "type": row["type"], "must_abstain": row["must_abstain"],
            "recall@3": r3, "recall@5": r5, "citation_validity": cv,
            "answered_from_context": answered_from_context, "confidence": resp.get("confidence"),
            "abstention_correct": (not answered_from_context) if row["must_abstain"] else None,
            "expect_found": hit, "cached": bool(resp.get("cached")),
            "top_score": retrieved[0].get("score") if retrieved else None,
            "retrieved": [f"{r.get('source')} / {r.get('section')}" for r in retrieved],
            "answer": resp.get("answer"), "seconds": row.get("seconds"),
        })
    return {
        "recall@3": _mean(r3s),
        "recall@5": _mean(r5s),
        "recall@5_by_type": {t: {"recall@5": _mean(v), "questions": len(v)}
                             for t, v in sorted(by_type.items())},
        "citation_validity": round(valid / cited, 4) if cited else None,
        "citations": cited,
        "abstention_correctness": round(abstain_ok / abstain_total, 4) if abstain_total
        else None,
        "must_abstain_questions": abstain_total,
        "answer_rate_on_answerable": round(answered / answerable, 4) if answerable else None,
        "expect_found_rate": round(expect_hit / expect_total, 4) if expect_total else None,
        "per_question": per_question,
    }


def gate(results: dict, baseline: dict | None) -> list[str]:
    """Return one message per failed gate (section 5.4); an empty list means pass."""
    failures = []
    recall5 = results.get("recall@5")
    if baseline and baseline.get("recall@5") is not None and recall5 is not None:
        floor = baseline["recall@5"] - MAX_RECALL5_DROP
        if recall5 < floor - 1e-9:
            failures.append(f"recall@5 {recall5:.3f} is more than {MAX_RECALL5_DROP:.0%} below "
                            f"the baseline {baseline['recall@5']:.3f}")
    abstention = results.get("abstention_correctness")
    if abstention is not None and abstention < REQUIRED_ABSTENTION:
        wrong = [q["id"] for q in results.get("per_question", [])
                 if q["must_abstain"] and q["answered_from_context"]]
        failures.append(f"abstention correctness {abstention:.0%}: must-abstain questions "
                        f"answered from context: {', '.join(wrong)}")
    validity = results.get("citation_validity")
    if validity is not None and validity < MIN_CITATION_VALIDITY:
        failures.append(f"citation validity {validity:.1%} is below "
                        f"{MIN_CITATION_VALIDITY:.0%}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate RAHBAR against a golden set.")
    parser.add_argument("--api", required=True, help="base URL, e.g. http://192.168.49.2")
    parser.add_argument("--host", help="Host header, when your Ingress rule names a host")
    parser.add_argument("--golden", type=Path, default=HERE / "golden.yaml")
    parser.add_argument("--baseline", type=Path, default=HERE / "baseline.json")
    parser.add_argument("--out", type=Path, default=HERE / "results.json")
    parser.add_argument("--update-baseline", action="store_true",
                        help="write these results as the new baseline")
    args = parser.parse_args()

    questions = load_golden(args.golden)
    rows = []
    headers = {"Host": args.host} if args.host else {}
    with httpx.Client(timeout=120.0, headers=headers) as client:
        for q in questions:
            started = time.perf_counter()
            response = ask(client, args.api, q["question"])
            rows.append({
                "id": q["id"], "type": q.get("type", "ordinary"),
                "must_abstain": bool(q.get("must_abstain", False)),
                "relevant": q.get("relevant") or [],
                "expect": q.get("expect"),
                "response": response,
                "seconds": round(time.perf_counter() - started, 2),
            })
            print(f"  {q['id']:<5} {rows[-1]['seconds']:>6.2f}s  {q['question'][:60]}")

    try:
        results = summarise(rows)
        baseline = json.loads(args.baseline.read_text()) if args.baseline.exists() else None
        failures = gate(results, baseline)
    except NotImplementedError as exc:
        print(f"\nnot implemented yet: {exc}")
        return 2

    results["golden"] = str(args.golden)
    results["questions"] = len(rows)
    args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nresults written to {args.out}")
    if args.update_baseline:
        args.baseline.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"baseline updated: {args.baseline}")

    if failures:
        print("\nGATE FAILED")
        for message in failures:
            print(f"  - {message}")
        return 1
    print("\ngate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
