"""Completed-hours requirements. This is a LOCAL, manually-curated rule set, entirely
separate from UoS course-to-course prerequisites (see prerequisite_tools.py). A course
missing from this file has NO configured completed-hours rule -- that is not a failure,
it means the check does not apply (see evaluate_completed_hours)."""

import json
from pathlib import Path

from app.exceptions import KnowledgeBaseError

_KB_PATH = Path(__file__).resolve().parent.parent / "knowledge_base" / "course_completed_hours.json"


def _load_knowledge_base() -> dict:
    if not _KB_PATH.exists():
        raise KnowledgeBaseError(f"Completed-hours knowledge base file not found at {_KB_PATH}.")
    try:
        with open(_KB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise KnowledgeBaseError(f"Completed-hours knowledge base file is malformed JSON: {exc}") from exc

    if not isinstance(data.get("courses"), list):
        raise KnowledgeBaseError("Completed-hours knowledge base is missing a top-level 'courses' list.")
    return data


def get_completed_hours_requirement(course_id: int) -> dict:
    data = _load_knowledge_base()
    for course in data["courses"]:
        # Course_ID is a text column in the database while the knowledge base stores ints,
        # so compare as strings -- otherwise the rule silently never matches.
        if str(course.get("course_id")) == str(course_id):
            return {
                "course_id": course_id,
                "found": True,
                "course_name": course.get("course_name"),
                "minimum_completed_hours": course.get("minimum_completed_hours"),
                "required_standing": course.get("required_standing"),
                "status": course.get("status"),
            }
    return {"course_id": course_id, "found": False}


def _is_valid_hours(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def evaluate_completed_hours(student_completed_hours: int | None, requirement: dict) -> dict:
    """Per approved design: a course absent from the knowledge base is NOT APPLICABLE,
    never a rejection and never an automatic manual review.

    If a rule applies but the student's completed hours (nullable in the Student table) or
    the configured threshold is missing/invalid, the result is unresolved (satisfied=None),
    which routes to MANUAL_REVIEW -- never a guessed pass or fail."""
    if not requirement.get("found"):
        return {
            "applicable": False,
            "requirement_found": False,
            "satisfied": None,
            "student_completed_hours": student_completed_hours,
            "required_hours": None,
        }

    required_hours = requirement["minimum_completed_hours"]
    if not _is_valid_hours(student_completed_hours) or not _is_valid_hours(required_hours):
        return {
            "applicable": True,
            "requirement_found": True,
            "satisfied": None,
            "student_completed_hours": student_completed_hours,
            "required_hours": required_hours,
            "required_standing": requirement.get("required_standing"),
            "error": (
                "Student completed hours or the configured required hours is missing or invalid; "
                "cannot evaluate the completed-hours requirement."
            ),
        }

    return {
        "applicable": True,
        "requirement_found": True,
        "satisfied": student_completed_hours >= required_hours,
        "student_completed_hours": student_completed_hours,
        "required_hours": required_hours,
        "required_standing": requirement.get("required_standing"),
    }
