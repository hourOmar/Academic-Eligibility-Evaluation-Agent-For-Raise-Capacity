"""Deterministic mapping from the eligibility evidence to rejection keywords.

Only CONFIRMED failures produce a keyword, using exactly the same per-check booleans the
Academic Eligibility Evaluation Agent computed (nothing is re-evaluated here):

    prerequisite     verified is True AND satisfied is False
    completed_hours  applicable is True AND satisfied is False
    capacity         satisfied is False

Unresolved evidence (satisfied is None, or the prerequisite could not be verified) is
reported separately by identify_unresolved_checks() and is never turned into a reason.
"""

import json
from pathlib import Path

from app.exceptions import KnowledgeBaseError
from app.tools.hours_tools import get_completed_hours_requirement

_KB_PATH = Path(__file__).resolve().parent.parent / "knowledge_base" / "rejection_keywords.json"


def load_rejection_knowledge_base() -> dict:
    if not _KB_PATH.exists():
        raise KnowledgeBaseError(f"Rejection keyword knowledge base not found at {_KB_PATH}.")
    try:
        with open(_KB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise KnowledgeBaseError(f"Rejection keyword knowledge base is malformed JSON: {exc}") from exc
    for key in ("keywords", "topic_terms", "fallback_templates"):
        if not isinstance(data.get(key), dict):
            raise KnowledgeBaseError(f"Rejection keyword knowledge base is missing a '{key}' object.")
    return data


_APPROVED_RULE_STATUS = "approved"


def _format_ids(ids) -> str:
    return ", ".join(str(i) for i in ids)


def _format_options(groups) -> str:
    """[[1501211], [1501215, 1501279]] -> '1501211 or (1501215 and 1501279)'."""
    parts = []
    for group in groups:
        ids = " and ".join(str(i) for i in group)
        parts.append(f"({ids})" if len(group) > 1 else ids)
    return " or ".join(parts)


def _failure(vocabulary: dict, keyword: str, facts: dict, evidence: dict, variant: str | None = None) -> dict:
    entry = {**vocabulary[keyword], **(vocabulary[keyword].get("variants", {}).get(variant) or {})}
    return {
        "keyword": keyword,
        "requirement": entry["requirement"],
        "label": entry["label"],
        "policy_basis": entry["policy_basis"],
        "plain_reason": entry["plain_reason"].format(**facts),
        "concise_reason": entry["concise_reason"].format(**facts),
        "supportive_guidance": entry.get("supportive_guidance", "").format(**facts),
        "evidence": evidence,
    }


def _prerequisite_failures(prereq: dict, vocabulary: dict) -> list[dict]:
    """Uses the relationship type the eligibility agent already parsed from UoS:

    SINGLE / AND -> every listed course is required, so each missing course is a reason.
    OR           -> every option failed (that is what satisfied=False means for OR), but
                    any ONE option would be enough, so it is a single reason that lists the
                    options and never implies that all of them are required.
    """
    evidence = {
        "raw_prerequisite_text": prereq.get("raw_prerequisite_text"),
        "relationship_type": prereq.get("relationship_type"),
        "required_prerequisite_groups": prereq.get("required_prerequisite_groups"),
        "source_url": prereq.get("source_url"),
    }
    never_attempted = prereq.get("never_attempted") or []
    not_passed = prereq.get("failed_or_incomplete_courses") or []
    groups = prereq.get("required_prerequisite_groups") or []

    if prereq.get("relationship_type") == "OR" and groups and (never_attempted or not_passed):
        entry = vocabulary["prerequisite_options_not_satisfied"]
        details = []
        if never_attempted:
            details.append(entry["status_never_attempted"].format(course_ids=_format_ids(never_attempted)))
        if not_passed:
            details.append(entry["status_not_passed"].format(course_ids=_format_ids(not_passed)))
        facts = {"options": _format_options(groups), "status_details": "; ".join(details)}
        return [_failure(vocabulary, "prerequisite_options_not_satisfied", facts, {
            **evidence, "never_attempted": never_attempted, "failed_or_incomplete_courses": not_passed,
        })]

    failures = []
    if never_attempted:
        failures.append(_failure(
            vocabulary, "prerequisite_not_completed", {"course_ids": _format_ids(never_attempted)},
            {**evidence, "never_attempted": never_attempted},
        ))
    if not_passed:
        failures.append(_failure(
            vocabulary, "prerequisite_not_passed", {"course_ids": _format_ids(not_passed)},
            {**evidence, "failed_or_incomplete_courses": not_passed},
        ))
    return failures


def _completed_hours_rule_status(course_id) -> str | None:
    """The eligibility evidence does not carry the rule's status, so it is read from the
    same local knowledge base the eligibility agent used (read-only, same function)."""
    if course_id is None:
        return None
    return get_completed_hours_requirement(course_id).get("status")


def identify_confirmed_failures(evidence_package: dict, knowledge_base: dict) -> list[dict]:
    vocabulary = knowledge_base["keywords"]
    checks = evidence_package.get("checks", {})
    prereq = checks.get("prerequisite") or {}
    hours = checks.get("completed_hours") or {}
    capacity = checks.get("capacity") or {}
    failures = []

    if prereq.get("verified") is True and prereq.get("satisfied") is False:
        failures.extend(_prerequisite_failures(prereq, vocabulary))

    if hours.get("applicable") is True and hours.get("satisfied") is False:
        facts = {
            "student_completed_hours": hours.get("student_completed_hours"),
            "required_hours": hours.get("required_hours"),
        }
        rule_status = _completed_hours_rule_status((evidence_package.get("course") or {}).get("Course_ID"))
        is_approved = rule_status == _APPROVED_RULE_STATUS
        failure = _failure(
            vocabulary, "completed_hours_insufficient", facts, {**facts, "rule_status": rule_status},
            variant=None if is_approved else "development_rule",
        )
        failure["requires_policy_confirmation"] = not is_approved
        failures.append(failure)

    if capacity.get("satisfied") is False:
        facts = {
            "section_id": evidence_package.get("section_id"),
            "current_capacity": capacity.get("current_capacity"),
            "maximum_allowed_capacity": capacity.get("maximum_allowed_capacity"),
        }
        failures.append(_failure(vocabulary, "no_remaining_capacity", facts, {
            **facts,
            "total_capacity": capacity.get("total_capacity"),
            "additional_capacity": capacity.get("additional_capacity"),
            "remaining_raise_capacity": capacity.get("remaining_raise_capacity"),
        }))

    return failures


def identify_unresolved_checks(evidence_package: dict) -> list[dict]:
    """Checks that could not be conclusively evaluated. These are evidence for manual
    review, never rejection reasons."""
    checks = evidence_package.get("checks", {})
    prereq = checks.get("prerequisite") or {}
    hours = checks.get("completed_hours") or {}
    capacity = checks.get("capacity") or {}
    unresolved = []

    if prereq.get("verified") is not True:
        unresolved.append({"check": "prerequisite", "issue": "not_verified", "details": prereq.get("details")})
    elif prereq.get("satisfied") is None:
        unresolved.append({
            "check": "prerequisite", "issue": "unresolved",
            "details": prereq.get("details"),
            "unresolved_grade_courses": prereq.get("unresolved_grade_courses") or [],
        })

    if hours.get("applicable") is True and hours.get("satisfied") is None:
        unresolved.append({"check": "completed_hours", "issue": "unresolved", "details": hours.get("error")})

    if capacity.get("satisfied") is None:
        unresolved.append({"check": "capacity", "issue": "unresolved", "details": capacity.get("error")})

    return unresolved
