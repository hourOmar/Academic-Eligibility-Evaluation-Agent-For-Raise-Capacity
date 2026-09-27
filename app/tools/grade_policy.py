"""Centralized prerequisite-passing-grade policy.

Approved rule (University of Sharjah undergraduate grading policy, minimum passing grade
for Bachelor's students = D):

    Passing:  A, B+, B, C+, C, D+, D
    Failing:  F, WF, FA
    NULL:     course still in progress / not completed -> does NOT satisfy a prerequisite

The mapping lives in app/knowledge_base/grade_policy.json, not hardcoded here, so it can
be updated later without touching evaluation logic.
"""

import json
from pathlib import Path

from app.exceptions import KnowledgeBaseError

_POLICY_PATH = Path(__file__).resolve().parent.parent / "knowledge_base" / "grade_policy.json"

_policy_cache: dict | None = None


def _load_policy() -> dict:
    global _policy_cache
    if _policy_cache is not None:
        return _policy_cache

    if not _POLICY_PATH.exists():
        raise KnowledgeBaseError(f"Grade policy file not found at {_POLICY_PATH}.")
    try:
        with open(_POLICY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise KnowledgeBaseError(f"Grade policy file is malformed JSON: {exc}") from exc

    if not isinstance(data.get("passing_grades"), list) or not isinstance(data.get("failing_grades"), list):
        raise KnowledgeBaseError("Grade policy file must define 'passing_grades' and 'failing_grades' lists.")

    data["_passing_set"] = {g.strip().upper() for g in data["passing_grades"]}
    data["_failing_set"] = {g.strip().upper() for g in data["failing_grades"]}
    _policy_cache = data
    return data


def classify_grade(grade: str | None) -> bool | None:
    """Returns:
    True  -> grade counts as passing under the approved policy.
    False -> grade does NOT count as passing (explicit failing grade, or NULL/in-progress).
    None  -> grade value is not covered by the current policy -- unresolved, never guessed.
    """
    policy = _load_policy()
    if grade is None:
        return False
    normalized = grade.strip().upper()
    if normalized in policy["_passing_set"]:
        return True
    if normalized in policy["_failing_set"]:
        return False
    return None
