"""Course-to-course prerequisites, retrieved from the official University of Sharjah
Computer Science degree page (confirmed server-rendered HTML -- see README).

This is DELIBERATELY separate from the local completed-hours knowledge base
(hours_tools.py): a prerequisite is a course-to-course relationship verified from UoS;
a completed-hours rule is a manually-curated local policy. They are never merged here.

Text conditions such as "Junior Standing" that appear alongside a course-ID prerequisite
(e.g. "1501215 and Junior Standing") are extracted into `additional_text_conditions` and
are NEVER converted into a credit-hour number -- that would be inventing a rule.
"""

import re

import requests
from bs4 import BeautifulSoup

from app import config
from app.exceptions import PrerequisiteSourceUnavailableError
from app.tools.grade_policy import classify_grade

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AcademicEligibilityAgent/1.0; +standalone-prototype)"}
_REQUEST_TIMEOUT_SECONDS = 20
_COURSE_ID_PATTERN = re.compile(r"\b\d{6,8}\b")

# Module-level cache of the parsed degree page, populated on first use.
_page_cache: dict | None = None


def _fetch_degree_requirements_table() -> dict:
    global _page_cache
    if _page_cache is not None:
        return _page_cache

    try:
        response = requests.get(config.UOS_CS_DEGREE_URL, headers=_HEADERS, timeout=_REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise PrerequisiteSourceUnavailableError(
            f"Could not reach the University of Sharjah degree page ({config.UOS_CS_DEGREE_URL}): {exc}"
        ) from exc

    soup = BeautifulSoup(response.text, "html.parser")
    container = soup.find(id="degree-requirements")
    if container is None:
        raise PrerequisiteSourceUnavailableError(
            "The 'Degree Requirements' section (id='degree-requirements') was not found on the "
            "UoS page. The page layout may have changed since this scraper was written."
        )

    courses: dict[int, dict] = {}
    conflicting_course_ids: set[int] = set()

    for table in container.find_all("table"):
        for row in table.find_all("tr"):
            cells = [cell.get_text(strip=True) for cell in row.find_all("td")]
            if len(cells) != 4:
                continue
            course_id_text, course_title, _credit_hours, prerequisite_text = cells
            if course_id_text.strip().lower() == "course #":
                continue  # header row
            if not course_id_text.strip().isdigit():
                continue  # not a course data row

            course_id = int(course_id_text.strip())
            raw_text = prerequisite_text.strip()
            parsed = _parse_prerequisite_text(raw_text)
            # Compare by PARSED meaning, not raw text: the page lists some courses twice
            # (once in the flat requirements table, once in the semester-by-semester plan)
            # and the wording order can differ ("1501215 and Junior Standing" vs.
            # "Junior Standing, 1501215") without actually disagreeing. Only a genuine
            # difference in required course IDs/relationship counts as a real conflict.
            normalized = (
                parsed["relationship_type"],
                tuple(sorted(tuple(sorted(group)) for group in parsed["prerequisite_groups"])),
                tuple(sorted(cond.lower() for cond in parsed["additional_text_conditions"])),
                parsed["is_pre_or_co"],
            )
            entry = {
                "course_name": course_title.strip(),
                "raw_prerequisite_text": raw_text,
                "parsed": parsed,
                "normalized": normalized,
            }

            existing = courses.get(course_id)
            if existing is not None and existing["normalized"] != normalized:
                # Genuinely different required course IDs/relationship across the two
                # listings -- do not silently pick one, flag it as unverifiable.
                conflicting_course_ids.add(course_id)
            else:
                courses.setdefault(course_id, entry)

    _page_cache = {"courses": courses, "conflicting_course_ids": conflicting_course_ids}
    return _page_cache


def _parse_prerequisite_text(raw_text: str) -> dict:
    text_value = raw_text.strip()

    if text_value.lower() == "none":
        return {
            "relationship_type": "NONE",
            "is_pre_or_co": False,
            "prerequisite_groups": [],
            "additional_text_conditions": [],
        }

    is_pre_or_co = False
    remainder = text_value
    pre_co_match = re.match(r"^\s*pre\s*/\s*co\s*:?\s*", remainder, flags=re.IGNORECASE)
    if pre_co_match:
        is_pre_or_co = True
        remainder = remainder[pre_co_match.end():]

    # Top-level OR split. Each branch may itself contain an AND-ed set of course IDs.
    or_branches = re.split(r"\bor\b", remainder, flags=re.IGNORECASE)
    prerequisite_groups: list[list[int]] = []
    for branch in or_branches:
        ids_in_branch = [int(match) for match in _COURSE_ID_PATTERN.findall(branch)]
        if ids_in_branch:
            prerequisite_groups.append(ids_in_branch)

    # Whatever text is left after removing every course-ID token and connector word is a
    # free-text condition (e.g. "Junior Standing"). Kept separate, never evaluated as a
    # course prerequisite and never turned into a completed-hours number.
    residual = _COURSE_ID_PATTERN.sub(" ", remainder)
    residual_parts = re.split(r"\band\b|\bor\b|,", residual, flags=re.IGNORECASE)
    additional_text_conditions = [part.strip() for part in residual_parts if part.strip()]

    if not prerequisite_groups:
        relationship_type = "TEXT_ONLY"
    elif len(prerequisite_groups) == 1:
        relationship_type = "AND" if len(prerequisite_groups[0]) > 1 else "SINGLE"
    else:
        relationship_type = "OR"

    return {
        "relationship_type": relationship_type,
        "is_pre_or_co": is_pre_or_co,
        "prerequisite_groups": prerequisite_groups,
        "additional_text_conditions": additional_text_conditions,
    }


def get_course_prerequisites(course_id: int) -> dict:
    """Returns a verified=True structured result, or verified=False with an explanatory
    error -- never a guessed prerequisite."""
    course_id = int(course_id)  # defend against being called with a string, e.g. "1501215"
    try:
        page_data = _fetch_degree_requirements_table()
    except PrerequisiteSourceUnavailableError as exc:
        return {"course_id": course_id, "verified": False, "error": str(exc)}

    if course_id in page_data["conflicting_course_ids"]:
        return {
            "course_id": course_id,
            "verified": False,
            "error": (
                f"Conflicting prerequisite text found for course {course_id} across the UoS "
                "degree-requirements tables; cannot verify which is authoritative."
            ),
        }

    entry = page_data["courses"].get(course_id)
    if entry is None:
        return {
            "course_id": course_id,
            "verified": False,
            "error": f"Course {course_id} was not found in the UoS Degree Requirements tables.",
        }

    parsed = entry["parsed"]
    return {
        "course_id": course_id,
        "course_name": entry["course_name"],
        "raw_prerequisite_text": entry["raw_prerequisite_text"],
        "relationship_type": parsed["relationship_type"],
        "is_pre_or_co": parsed["is_pre_or_co"],
        "prerequisite_groups": parsed["prerequisite_groups"],
        "additional_text_conditions": parsed["additional_text_conditions"],
        "source": "University of Sharjah",
        "source_url": config.UOS_CS_DEGREE_URL,
        "verified": True,
    }


def _course_pass_status(course_id: int, transcript: list[dict]) -> str:
    """PASSED / NOT_PASSED / UNRESOLVED / NEVER_ATTEMPTED for one required course, using the
    approved grade policy (app/tools/grade_policy.py). If the course was attempted more than
    once, it counts as PASSED if any recorded attempt has a qualifying grade."""
    attempts = [row for row in transcript if row["Course_ID"] == course_id]
    if not attempts:
        return "NEVER_ATTEMPTED"

    classifications = [classify_grade(row["Grade"]) for row in attempts]
    if any(c is True for c in classifications):
        return "PASSED"
    if any(c is None for c in classifications):
        return "UNRESOLVED"
    return "NOT_PASSED"  # every attempt was an explicit failing grade or NULL/in-progress


def evaluate_prerequisite_status(prerequisite_data: dict, transcript: list[dict]) -> dict:
    """Deterministic comparison against the transcript, using the approved grade policy.

    A required course counts as satisfied only if the student has a recorded PASSING grade
    for it (per grade_policy.classify_grade). Never attempted, an explicit failing grade,
    and NULL/in-progress are all treated as NOT satisfying the prerequisite. A grade value
    outside the approved policy (neither listed as passing nor failing) is never guessed --
    it is reported as unresolved and routes to MANUAL_REVIEW.
    """
    additional_text_conditions = prerequisite_data.get("additional_text_conditions", [])

    if not prerequisite_data.get("verified"):
        return {
            "verified": False,
            "relationship_type": prerequisite_data.get("relationship_type"),
            "required_prerequisite_groups": [],
            "matched_transcript_courses": [],
            "passed_courses": [],
            "failed_or_incomplete_courses": [],
            "never_attempted": [],
            "unresolved_grade_courses": [],
            "prerequisite_satisfied": None,
            "needs_manual_review": True,
            "additional_text_conditions": additional_text_conditions,
            "reason": prerequisite_data.get("error", "Prerequisite could not be verified from the UoS source."),
        }

    relationship_type = prerequisite_data["relationship_type"]

    if relationship_type == "NONE":
        return {
            "verified": True,
            "relationship_type": relationship_type,
            "required_prerequisite_groups": [],
            "matched_transcript_courses": [],
            "passed_courses": [],
            "failed_or_incomplete_courses": [],
            "never_attempted": [],
            "unresolved_grade_courses": [],
            "prerequisite_satisfied": True,
            "needs_manual_review": False,
            "additional_text_conditions": additional_text_conditions,
            "reason": "University of Sharjah source explicitly lists no prerequisite for this course.",
        }

    if relationship_type == "TEXT_ONLY":
        return {
            "verified": True,
            "relationship_type": relationship_type,
            "required_prerequisite_groups": [],
            "matched_transcript_courses": [],
            "passed_courses": [],
            "failed_or_incomplete_courses": [],
            "never_attempted": [],
            "unresolved_grade_courses": [],
            "prerequisite_satisfied": None,
            "needs_manual_review": True,
            "additional_text_conditions": additional_text_conditions,
            "reason": (
                "The UoS prerequisite is expressed only as a text condition (no course ID), "
                "which this prototype cannot evaluate automatically."
            ),
        }

    # Course_ID can come back from the database as a string, while prerequisite_groups
    # (parsed from the UoS page by regex) are always real ints. Without normalizing here,
    # every comparison below silently fails -- e.g. "1501211" == 1501211 is False in
    # Python -- making a course the student actually passed look like it was never
    # attempted. This bit us for real: Omar Haddad (U25199999) has a recorded B in
    # 1501211, but was reported as never having attempted it, until this fix.
    transcript = [{**row, "Course_ID": int(row["Course_ID"])} for row in transcript]

    prerequisite_groups = prerequisite_data["prerequisite_groups"]
    all_required_ids = sorted({cid for group in prerequisite_groups for cid in group})
    transcript_ids_present = {row["Course_ID"] for row in transcript}
    matched_transcript_courses = [cid for cid in all_required_ids if cid in transcript_ids_present]

    branch_reports = []
    for group in prerequisite_groups:
        statuses = {cid: _course_pass_status(cid, transcript) for cid in group}
        if all(status == "PASSED" for status in statuses.values()):
            branch_state = "SATISFIED"
        elif any(status in ("NEVER_ATTEMPTED", "NOT_PASSED") for status in statuses.values()):
            branch_state = "FAILED"
        else:
            branch_state = "UNRESOLVED"
        branch_reports.append({"group": group, "statuses": statuses, "branch_state": branch_state})

    satisfied_branch = next((b for b in branch_reports if b["branch_state"] == "SATISFIED"), None)
    if satisfied_branch is not None:
        return {
            "verified": True,
            "relationship_type": relationship_type,
            "required_prerequisite_groups": prerequisite_groups,
            "matched_transcript_courses": matched_transcript_courses,
            "passed_courses": list(satisfied_branch["group"]),
            "failed_or_incomplete_courses": [],
            "never_attempted": [],
            "unresolved_grade_courses": [],
            "prerequisite_satisfied": True,
            "needs_manual_review": False,
            "additional_text_conditions": additional_text_conditions,
            "reason": (
                f"Required course(s) {satisfied_branch['group']} were passed with a qualifying "
                "grade under the approved grade policy."
            ),
        }

    unresolved_branch = next((b for b in branch_reports if b["branch_state"] == "UNRESOLVED"), None)
    if unresolved_branch is not None:
        unresolved_ids = [cid for cid, status in unresolved_branch["statuses"].items() if status == "UNRESOLVED"]
        return {
            "verified": True,
            "relationship_type": relationship_type,
            "required_prerequisite_groups": prerequisite_groups,
            "matched_transcript_courses": matched_transcript_courses,
            "passed_courses": [],
            "failed_or_incomplete_courses": [],
            "never_attempted": [],
            "unresolved_grade_courses": unresolved_ids,
            "prerequisite_satisfied": None,
            "needs_manual_review": True,
            "additional_text_conditions": additional_text_conditions,
            "reason": (
                f"Course(s) {unresolved_ids} have a recorded grade value that is not covered by "
                "the approved grade policy (neither listed as passing nor failing); cannot confirm."
            ),
        }

    # Every branch is FAILED.
    never_attempted_all = sorted(
        {cid for b in branch_reports for cid, status in b["statuses"].items() if status == "NEVER_ATTEMPTED"}
    )
    not_passed_all = sorted(
        {cid for b in branch_reports for cid, status in b["statuses"].items() if status == "NOT_PASSED"}
    )
    return {
        "verified": True,
        "relationship_type": relationship_type,
        "required_prerequisite_groups": prerequisite_groups,
        "matched_transcript_courses": matched_transcript_courses,
        "passed_courses": [],
        "failed_or_incomplete_courses": not_passed_all,
        "never_attempted": never_attempted_all,
        "unresolved_grade_courses": [],
        "prerequisite_satisfied": False,
        "needs_manual_review": False,
        "additional_text_conditions": additional_text_conditions,
        "reason": (
            f"Required prerequisite course(s) not satisfied: never attempted {never_attempted_all}; "
            f"attempted without a passing grade (failing grade or still in progress) {not_passed_all}."
        ),
    }