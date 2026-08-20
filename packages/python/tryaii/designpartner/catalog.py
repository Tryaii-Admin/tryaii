"""Questionnaire catalog: loading, ask_if evaluation, answer validation
(shared/designpartner/SPEC.md §3).

Lenient toward the agent, strict about honesty: every problem is reported
at once, a condition-skipped answer is dropped with a warning, and nothing
is ever guessed or filled in.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

_QUESTIONS_PATH = Path(__file__).parent / "data" / "questions.json"
_catalog_cache: Optional[dict] = None


def load_catalog() -> dict:
    global _catalog_cache
    if _catalog_cache is None:
        _catalog_cache = json.loads(_QUESTIONS_PATH.read_text(encoding="utf-8"))
    return _catalog_cache


def all_questions(catalog: dict) -> list[dict]:
    """Flat question list in catalog order."""
    return [q for section in catalog["sections"] for q in section["questions"]]


def tiers_by_id(catalog: dict) -> dict:
    return {tier["id"]: tier for tier in catalog["consent_tiers"]}


def _present(answers: dict, qid: str) -> bool:
    """An answer counts as present when the key exists and the value is not
    an empty string / empty list (SPEC §3: '' and [] are treated as absent)."""
    if qid not in answers:
        return False
    value = answers[qid]
    return value != "" and value != []


def _condition_met(cond: Optional[dict], answers: dict) -> bool:
    if cond is None:
        return True
    qid = cond["question"]
    op = cond["op"]
    if op == "answered":
        return _present(answers, qid)
    if not _present(answers, qid):
        return False
    value = answers[qid]
    if op == "equals":
        return value == cond["value"]
    if op == "contains":
        return isinstance(value, list) and cond["value"] in value
    return False


def applicable_questions(catalog: dict, answers: dict) -> list[str]:
    """Ids of questions whose ask_if is met (single forward pass — a
    condition may only reference an earlier question, SPEC §3.1)."""
    return [q["id"] for q in all_questions(catalog)
            if _condition_met(q.get("ask_if"), answers)]


def _valid_email(value: str) -> bool:
    """SPEC §3.2: contains at least one '@' with non-empty text on both sides."""
    at = value.find("@")
    return 0 < at < len(value) - 1


def _type_problem(question: dict, value: Any) -> Optional[str]:
    """Problem code for a present answer of the wrong shape, else None."""
    qtype = question["type"]
    if qtype in ("text", "select"):
        if not isinstance(value, str):
            return "invalid_type"
        if qtype == "select":
            option_ids = [o["id"] for o in question["options"]]
            if value not in option_ids:
                return "unknown_option"
        elif question.get("format") == "email" and not _valid_email(value):
            return "invalid_email"
    elif qtype == "multi_select":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return "invalid_type"
        option_ids = [o["id"] for o in question["options"]]
        if any(v not in option_ids for v in value):
            return "unknown_option"
    elif qtype == "boolean":
        if not isinstance(value, bool):
            return "invalid_type"
    return None


def validate_answers(catalog: dict, answers: dict) -> dict:
    """SPEC §3.2 — returns {"answers": canonical, "problems": [...],
    "warnings": [...]}. ALL problems reported at once; canonical answers
    are the applicable, present, valid ones in catalog order."""
    questions = all_questions(catalog)
    known_ids = {q["id"] for q in questions}

    canonical: dict[str, Any] = {}
    problems: list[dict] = []
    warnings: list[dict] = []

    # Single forward pass: conditions are evaluated against the answers as
    # given (a condition may only reference an earlier question).
    for question in questions:
        qid = question["id"]
        applicable = _condition_met(question.get("ask_if"), answers)
        present = _present(answers, qid)

        if not applicable:
            if present:
                warnings.append({
                    "question": qid,
                    "code": "not_applicable",
                    "message": f"'{qid}' does not apply to these answers — dropped",
                })
            continue

        if not present:
            if question["required"]:
                problems.append({
                    "question": qid,
                    "code": "missing",
                    "message": f"'{qid}' is required",
                })
            continue

        code = _type_problem(question, answers[qid])
        if code is not None:
            messages = {
                "invalid_type": f"'{qid}' has the wrong type for a {question['type']} question",
                "unknown_option": f"'{qid}' contains a value that is not one of its option ids",
                "invalid_email": f"'{qid}' must look like an email address",
            }
            problems.append({"question": qid, "code": code, "message": messages[code]})
            continue

        canonical[qid] = answers[qid]

    for qid in answers:
        if qid not in known_ids:
            problems.append({
                "question": qid,
                "code": "unknown_question",
                "message": f"'{qid}' is not in the questionnaire",
            })

    return {"answers": canonical, "problems": problems, "warnings": warnings}
