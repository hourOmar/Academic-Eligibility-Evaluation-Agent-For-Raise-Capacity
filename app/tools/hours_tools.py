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
        if course.get("course_id") == course_id:
            return {
                "course_id": course_id,
                "found": True,
                "course_name": course.get("course_name"),
                "minimum_completed_hours": course.get("minimum_completed_hours"),
                "required_standing": course.get("required_standing"),
                "status": course.get("status"),
            }
    return {"course_id": course_id, "found": False}


def evaluate_completed_hours(student_completed_hours: int, requirement: dict) -> dict:
    """Per approved design: a course absent from the knowledge base is NOT APPLICABLE,
    never a rejection and never an automatic manual review."""
    if not requirement.get("found"):
        return {
            "applicable": False,
            "requirement_found": False,
            "satisfied": None,
            "student_completed_hours": student_completed_hours,
            "required_hours": None,
        }

    required_hours = requirement["minimum_completed_hours"]
    return {
        "applicable": True,
        "requirement_found": True,
        "satisfied": student_completed_hours >= required_hours,
        "student_completed_hours": student_completed_hours,
        "required_hours": required_hours,
        "required_standing": requirement.get("required_standing"),
    }
