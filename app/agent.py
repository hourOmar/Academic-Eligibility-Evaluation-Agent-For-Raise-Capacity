"""Raise Capacity eligibility workflow.

Fixed pipeline (Gemini never chooses which tool to call, and is not given tool access):

    Request ID
    -> get request details
    -> get student academic info
    -> get transcript
    -> retrieve prerequisite from UoS
    -> evaluate prerequisite (incl. approved grade policy)
    -> get completed-hours requirement from local knowledge base
    -> evaluate completed-hours requirement
    -> get section capacity
    -> evaluate capacity (deterministic arithmetic)
    -> build evidence package
    -> send evidence package + explicit decision rules to Gemini
    -> Gemini decides APPROVE / REJECT / MANUAL_REVIEW and explains it
    -> return Gemini's recommendation and explanation

IMPORTANT (approved architecture): Python only collects and computes verified evidence --
it does NOT combine that evidence into a final recommendation. Gemini is the actual
decision-making layer; it receives the full evidence package plus the explicit decision
rules and produces both the recommendation and the reasoning. 

Run with:  python -m app.agent <request_id>
"""

import json
import sys

from app.exceptions import AgentError, CourseNotFoundError
from app.services.database import fetch_course
from app.services.gemini import get_gemini_decision
from app.tools.capacity_tools import evaluate_capacity, get_section_capacity
from app.tools.hours_tools import evaluate_completed_hours, get_completed_hours_requirement
from app.tools.prerequisite_tools import evaluate_prerequisite_status, get_course_prerequisites
from app.tools.request_tools import get_raise_capacity_request
from app.tools.student_tools import get_student_academic_info, get_student_transcript


def _build_evidence_package(request_id: str) -> dict:
    """Collects and computes all deterministic evidence. Does NOT decide a recommendation."""
    request_data = get_raise_capacity_request(request_id)
    student_id = request_data["Student_ID"]
    course_id = request_data["Course_ID"]
    section_id = request_data["Section_ID"]
    term = request_data["Term"]

    student = get_student_academic_info(student_id)
    transcript = get_student_transcript(student_id)

    course = fetch_course(course_id)
    if course is None:
        raise CourseNotFoundError(f"No Course found for Course_ID={course_id}.")

    section = get_section_capacity(section_id, course_id, term)

    prereq_data = get_course_prerequisites(course_id)
    prereq_eval = evaluate_prerequisite_status(prereq_data, transcript)

    hours_requirement = get_completed_hours_requirement(course_id)
    hours_eval = evaluate_completed_hours(student["Completed_Hours"], hours_requirement)

    capacity_eval = evaluate_capacity(section)

    checks = {
        "prerequisite": {
            "raw_prerequisite_text": prereq_data.get("raw_prerequisite_text"),
            "relationship_type": prereq_eval.get("relationship_type"),
            "is_pre_or_co": prereq_data.get("is_pre_or_co"),
            "required_prerequisite_groups": prereq_eval.get("required_prerequisite_groups"),
            "additional_text_conditions": prereq_eval.get("additional_text_conditions", []),
            "matched_transcript_courses": prereq_eval.get("matched_transcript_courses"),
            "passed_courses": prereq_eval.get("passed_courses"),
            "failed_or_incomplete_courses": prereq_eval.get("failed_or_incomplete_courses"),
            "never_attempted": prereq_eval.get("never_attempted"),
            "unresolved_grade_courses": prereq_eval.get("unresolved_grade_courses"),
            "satisfied": prereq_eval.get("prerequisite_satisfied"),
            "verified": prereq_eval.get("verified"),
            "source_url": prereq_data.get("source_url"),
            "details": prereq_eval.get("reason"),
        },
        "completed_hours": hours_eval,
        "capacity": capacity_eval,
    }

    return {
        "request_id": request_id,
        "student": {"Student_ID": student["Student_ID"], "Student_Name": student["Student_Name"]},
        "course": {"Course_ID": course["Course_ID"], "Course_Name": course["Course_Name"]},
        "section_id": section_id,
        "checks": checks,
    }


def evaluate_raise_capacity_request(request_id: str) -> dict:
    evidence_package = _build_evidence_package(request_id)

    decision = get_gemini_decision(evidence_package)

    return {
        "request_id": request_id,
        "course": evidence_package["course"],
        "evidence": evidence_package,
        "recommendation": decision["recommendation"],
        "reasoning": decision["reasoning"],
        "checks": decision["checks"],
        "gemini_status": decision["status"],
        "gemini_error": decision["error"],
        "gemini_model_used": decision.get("model_used"),
        "gemini_models_attempted": decision.get("models_attempted", []),
    }



# def _print_report(result: dict) -> None:
#     evidence_checks = result["evidence"]["checks"]
#     prereq = evidence_checks["prerequisite"]
#     hours = evidence_checks["completed_hours"]
#     capacity = evidence_checks["capacity"]

#     print(f"Request ID: {result['request_id']}")
#     print(f"Requested course: {result['course']['Course_ID']} - {result['course']['Course_Name']}")
#     print()
#     print("--- Deterministic evidence (computed in Python, sent to Gemini) ---")
#     print(f"Prerequisite (raw UoS text): {prereq.get('raw_prerequisite_text')}")
#     print(f"Prerequisite relationship type: {prereq.get('relationship_type')}")
#     print(f"Prerequisite verified: {prereq.get('verified')}")
#     print(f"Prerequisite satisfied: {prereq.get('satisfied')}")
#     print(f"Additional text conditions (not evaluated as hours/prereq): {prereq.get('additional_text_conditions')}")
#     print()
#     print(f"Student completed hours: {hours.get('student_completed_hours')}")
#     print(f"Completed-hours rule applicable: {hours.get('applicable')}")
#     print(f"Required hours (if applicable): {hours.get('required_hours')}")
#     print(f"Completed-hours satisfied: {hours.get('satisfied')}")
#     print()
#     print(f"Current capacity: {capacity.get('current_capacity')}")
#     print(f"Total capacity: {capacity.get('total_capacity')}")
#     print(f"Additional capacity: {capacity.get('additional_capacity')}")
#     print(f"Maximum allowed capacity: {capacity.get('maximum_allowed_capacity')}")
#     print(f"Remaining raise capacity: {capacity.get('remaining_raise_capacity')}")
#     print(f"Additional capacity available: {capacity.get('satisfied')}")
#     print()
#     print("--- Gemini decision (recommendation decided by Gemini, not Python) ---")
#     print(f"Gemini status: {result['gemini_status']}" + (f" ({result['gemini_error']})" if result["gemini_error"] else ""))
#     print(f"Recommendation: {result['recommendation']}")
#     print(f"Reasoning: {result['reasoning']}")
#     gemini_checks = result["checks"]
#     print(f"Gemini prerequisite explanation: {gemini_checks.get('prerequisite')}")
#     print(f"Gemini completed-hours explanation: {gemini_checks.get('completed_hours')}")
#     print(f"Gemini capacity explanation: {gemini_checks.get('capacity')}")


def _print_report(result: dict, show_json: bool = True) -> None:
    evidence = result["evidence"]
    student = evidence["student"]
    course = evidence["course"]
    prereq = evidence["checks"]["prerequisite"]
    hours = evidence["checks"]["completed_hours"]
    capacity = evidence["checks"]["capacity"]
    gemini_checks = result["checks"]

    print("=" * 70)
    print(" " * 20 + "RAISE CAPACITY ELIGIBILITY")
    print("=" * 70)

    # ------------------------------------------------------------------
    # REQUEST
    # ------------------------------------------------------------------
    print("\nREQUEST")
    print(f"  {'Request ID':<27}: {result['request_id']}")
    print(
        f"  {'Requested Course':<27}: "
        f"{course['Course_ID']} - {course['Course_Name']}"
    )
    print(f"  {'Section ID':<27}: {evidence['section_id']}")

    # ------------------------------------------------------------------
    # STUDENT
    # ------------------------------------------------------------------
    print("\nSTUDENT")
    print(f"  {'Student ID':<27}: {student['Student_ID']}")
    print(f"  {'Student Name':<27}: {student['Student_Name']}")

    # ------------------------------------------------------------------
    # DETERMINISTIC EVIDENCE
    # ------------------------------------------------------------------
    print("\n" + "-" * 70)
    print("DETERMINISTIC EVIDENCE")
    print("-" * 70)

    # Prerequisite
    print("\n[1] PREREQUISITE CHECK")
    print(
        f"  {'Raw UoS prerequisite':<27}: "
        f"{prereq.get('raw_prerequisite_text')}"
    )
    print(
        f"  {'Relationship type':<27}: "
        f"{prereq.get('relationship_type')}"
    )
    print(
        f"  {'Prerequisite verified':<27}: "
        f"{prereq.get('verified')}"
    )
    print(
        f"  {'Prerequisite satisfied':<27}: "
        f"{prereq.get('satisfied')}"
    )
    print(
        f"  {'Additional text conditions':<27}: "
        f"{prereq.get('additional_text_conditions')}"
    )

    # Completed hours
    print("\n[2] COMPLETED-HOURS CHECK")
    print(
        f"  {'Student completed hours':<27}: "
        f"{hours.get('student_completed_hours')}"
    )
    print(
        f"  {'Rule applicable':<27}: "
        f"{hours.get('applicable')}"
    )
    print(
        f"  {'Required hours':<27}: "
        f"{hours.get('required_hours')}"
    )
    print(
        f"  {'Requirement satisfied':<27}: "
        f"{hours.get('satisfied')}"
    )

    # Capacity
    print("\n[3] CAPACITY CHECK")
    print(
        f"  {'Current capacity':<27}: "
        f"{capacity.get('current_capacity')}"
    )
    print(
        f"  {'Total capacity':<27}: "
        f"{capacity.get('total_capacity')}"
    )
    print(
        f"  {'Additional capacity':<27}: "
        f"{capacity.get('additional_capacity')}"
    )
    print(
        f"  {'Maximum allowed capacity':<27}: "
        f"{capacity.get('maximum_allowed_capacity')}"
    )
    print(
        f"  {'Remaining raise capacity':<27}: "
        f"{capacity.get('remaining_raise_capacity')}"
    )
    print(
        f"  {'Additional capacity available':<27}: "
        f"{capacity.get('satisfied')}"
    )

    # ------------------------------------------------------------------
    # GEMINI DECISION
    # ------------------------------------------------------------------
    print("\n" + "-" * 70)
    print("GEMINI DECISION")
    print("-" * 70)

    gemini_status = result["gemini_status"]
    gemini_error = result["gemini_error"]

    print(f"\n  {'Gemini status':<27}: {gemini_status}")
    print(f"  {'Gemini model used':<27}: {result.get('gemini_model_used')}")
    attempted = result.get("gemini_models_attempted") or []
    if attempted:
        print(f"  {'Gemini models attempted':<27}: {', '.join(a['model'] for a in attempted)}")

    if gemini_error:
        print(f"  {'Gemini error':<27}: {gemini_error}")

    print(f"  {'Recommendation':<27}: {result['recommendation']}")

    print("\n  Reasoning:")
    print(f"    {result['reasoning']}")

    print("\n  Prerequisite explanation:")
    print(f"    {gemini_checks.get('prerequisite')}")

    print("\n  Completed-hours explanation:")
    print(f"    {gemini_checks.get('completed_hours')}")

    print("\n  Capacity explanation:")
    print(f"    {gemini_checks.get('capacity')}")

    # ------------------------------------------------------------------
    # FULL JSON RESULT
    # ------------------------------------------------------------------
    if not show_json:  # the combined pipeline (app/pipeline.py) prints its own JSON
        return

    print("\n" + "-" * 70)
    print("FULL JSON RESULT")
    print("-" * 70)

    print(json.dumps(result, indent=2, default=str))


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python -m app.agent <request_id>")
        sys.exit(1)
    request_id = sys.argv[1]  # Request_ID is a string key (e.g. "STUD_RSCA_RSCA_000004"), not numeric

    try:
        result = evaluate_raise_capacity_request(request_id)
    except AgentError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    _print_report(result)

if __name__ == "__main__":
    main()
