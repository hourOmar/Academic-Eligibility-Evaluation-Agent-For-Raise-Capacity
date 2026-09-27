"""Automated tests for the deterministic eligibility logic.

No database, Gemini API key, or network access is needed: the database connection, the
UoS degree page, and the Gemini client are all replaced with fakes.

Run with:  python -m pytest
"""

import pytest
import requests

from app import agent
from app.exceptions import NotRaiseCapacityRequestError, RequestNotFoundError
from app.services import gemini
from app.tools import prerequisite_tools, request_tools
from app.tools.capacity_tools import evaluate_capacity
from app.tools.grade_policy import classify_grade
from app.tools.hours_tools import evaluate_completed_hours, get_completed_hours_requirement
from app.tools.prerequisite_tools import evaluate_prerequisite_status, get_course_prerequisites


# ----------------------------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------------------------

_FAKE_UOS_PAGE = """
<html><body><div id="degree-requirements"><table>
<tr><td>Course #</td><td>Course Title</td><td>Cr. Hr.</td><td>Prerequisite</td></tr>
<tr><td>1501116</td><td>Programming I</td><td>3</td><td>None</td></tr>
<tr><td>1501215</td><td>Data Structures</td><td>3</td><td>1501211</td></tr>
<tr><td>1501370</td><td>AND Example</td><td>3</td><td>1501215, 1501279</td></tr>
<tr><td>1501445</td><td>OR Example</td><td>3</td><td>1501211 or 1501341</td></tr>
<tr><td>1501443</td><td>Human Computer Interaction</td><td>3</td><td>1501341, (1501246 or 1501247)</td></tr>
</table></div></body></html>
"""


class _FakeResponse:
    text = _FAKE_UOS_PAGE

    def raise_for_status(self):
        pass


@pytest.fixture
def fake_uos_page(monkeypatch):
    monkeypatch.setattr(prerequisite_tools, "_page_cache", None)
    monkeypatch.setattr(prerequisite_tools.requests, "get", lambda *args, **kwargs: _FakeResponse())


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _FakeConnection:
    """Returns the queued rows in order, one per execute() call."""

    def __init__(self, rows):
        self._rows = list(rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, _query, _params=None):
        return _FakeResult(self._rows.pop(0))


def _transcript(*rows):
    return [{"Course_ID": course_id, "Grade": grade} for course_id, grade in rows]


# ----------------------------------------------------------------------------------------
# Completed-hours requirement
# ----------------------------------------------------------------------------------------

def test_data_structures_has_completed_hours_rule():
    requirement = get_completed_hours_requirement(1501215)
    assert requirement["found"] is True
    assert requirement["course_name"] == "Data Structures"
    assert requirement["minimum_completed_hours"] == 31


def test_string_database_course_id_matches_numeric_knowledge_base_id():
    # Course_ID is a text column in the database.
    assert get_completed_hours_requirement("1501215")["found"] is True


def test_discrete_structures_does_not_get_data_structures_rule():
    assert get_completed_hours_requirement("1501279")["found"] is False


def test_course_without_rule_is_not_applicable():
    result = evaluate_completed_hours(10, get_completed_hours_requirement("1501116"))
    assert result["applicable"] is False
    assert result["satisfied"] is None


@pytest.mark.parametrize("hours, expected", [(30, False), (31, True), (32, True)])
def test_completed_hours_boundary(hours, expected):
    result = evaluate_completed_hours(hours, get_completed_hours_requirement("1501215"))
    assert result["applicable"] is True
    assert result["satisfied"] is expected


@pytest.mark.parametrize("hours", [None, -5, "40"])
def test_missing_or_invalid_completed_hours_is_unresolved(hours):
    result = evaluate_completed_hours(hours, get_completed_hours_requirement("1501215"))
    assert result["applicable"] is True
    assert result["satisfied"] is None
    assert "error" in result


# ----------------------------------------------------------------------------------------
# Capacity
# ----------------------------------------------------------------------------------------

def _capacity(current, total, additional):
    return evaluate_capacity(
        {"Current_Capacity": current, "Total_Capacity": total, "Additional_Capacity": additional}
    )


def test_capacity_available():
    result = _capacity(50, 50, 10)
    assert result["maximum_allowed_capacity"] == 60
    assert result["remaining_raise_capacity"] == 10
    assert result["satisfied"] is True


def test_capacity_exactly_at_maximum():
    result = _capacity(60, 50, 10)
    assert result["remaining_raise_capacity"] == 0
    assert result["satisfied"] is False


def test_capacity_one_seat_remaining():
    result = _capacity(59, 50, 10)
    assert result["remaining_raise_capacity"] == 1
    assert result["satisfied"] is True


@pytest.mark.parametrize(
    "current, total, additional",
    [(-3, 50, 10), (50, -1, 10), (50, 50, -10), (None, 50, 10), (50, None, 10)],
)
def test_invalid_capacity_is_unresolved(current, total, additional):
    result = _capacity(current, total, additional)
    assert result["satisfied"] is None
    assert "error" in result


# ----------------------------------------------------------------------------------------
# Grade policy and prerequisite evaluation
# ----------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "grade, expected",
    [("A", True), ("D", True), (" b+ ", True), ("F", False), ("WF", False), (None, False), ("W", None), ("I", None)],
)
def test_grade_classification(grade, expected):
    assert classify_grade(grade) is expected


def test_single_prerequisite_passed(fake_uos_page):
    data = get_course_prerequisites(1501215)
    result = evaluate_prerequisite_status(data, _transcript(("1501211", "B")))
    assert result["prerequisite_satisfied"] is True


def test_single_prerequisite_failed(fake_uos_page):
    data = get_course_prerequisites(1501215)
    result = evaluate_prerequisite_status(data, _transcript(("1501211", "F")))
    assert result["verified"] is True
    assert result["prerequisite_satisfied"] is False


def test_single_prerequisite_in_progress_null_grade(fake_uos_page):
    data = get_course_prerequisites(1501215)
    result = evaluate_prerequisite_status(data, _transcript(("1501211", None)))
    assert result["prerequisite_satisfied"] is False


def test_single_prerequisite_never_attempted(fake_uos_page):
    data = get_course_prerequisites(1501215)
    result = evaluate_prerequisite_status(data, [])
    assert result["prerequisite_satisfied"] is False
    assert result["never_attempted"] == [1501211]


def test_unrecognized_grade_is_unresolved(fake_uos_page):
    data = get_course_prerequisites(1501215)
    result = evaluate_prerequisite_status(data, _transcript(("1501211", "W")))
    assert result["prerequisite_satisfied"] is None
    assert result["needs_manual_review"] is True
    assert result["unresolved_grade_courses"] == [1501211]


def test_no_prerequisite(fake_uos_page):
    data = get_course_prerequisites(1501116)
    assert data["relationship_type"] == "NONE"
    assert evaluate_prerequisite_status(data, [])["prerequisite_satisfied"] is True


def test_and_prerequisite_requires_all_courses(fake_uos_page):
    data = get_course_prerequisites(1501370)
    assert data["relationship_type"] == "AND"
    only_one = evaluate_prerequisite_status(data, _transcript(("1501215", "A")))
    both = evaluate_prerequisite_status(data, _transcript(("1501215", "A"), ("1501279", "C")))
    assert only_one["prerequisite_satisfied"] is False
    assert both["prerequisite_satisfied"] is True


def test_or_prerequisite_requires_any_course(fake_uos_page):
    data = get_course_prerequisites(1501445)
    assert data["relationship_type"] == "OR"
    assert evaluate_prerequisite_status(data, _transcript(("1501341", "C")))["prerequisite_satisfied"] is True
    assert evaluate_prerequisite_status(data, [])["prerequisite_satisfied"] is False


def test_nested_prerequisite_is_unverified_not_satisfied(fake_uos_page):
    # 1501443 = "1501341, (1501246 or 1501247)". Passing only 1501247 (without 1501341)
    # must never be reported as satisfied.
    data = get_course_prerequisites(1501443)
    assert data["verified"] is False
    assert "Nested prerequisite expression not supported" in data["error"]

    result = evaluate_prerequisite_status(data, _transcript(("1501247", "A")))
    assert result["verified"] is False
    assert result["prerequisite_satisfied"] is None
    assert result["needs_manual_review"] is True


def test_uos_page_unreachable_is_unverified(monkeypatch):
    monkeypatch.setattr(prerequisite_tools, "_page_cache", None)

    def _raise(*args, **kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(prerequisite_tools.requests, "get", _raise)
    data = get_course_prerequisites(1501215)
    assert data["verified"] is False
    assert evaluate_prerequisite_status(data, [])["prerequisite_satisfied"] is None


# ----------------------------------------------------------------------------------------
# Request handling
# ----------------------------------------------------------------------------------------

def test_unknown_request_id(monkeypatch):
    monkeypatch.setattr(request_tools, "get_connection", lambda: _FakeConnection([None]))
    with pytest.raises(RequestNotFoundError):
        request_tools.get_raise_capacity_request("DOES_NOT_EXIST")


def test_wrong_request_type(monkeypatch):
    request_row = {
        "Request_ID": "R1", "Student_ID": "S1", "Request_Type": "Grade Change", "Request_Date": None,
        "Submission_Time": None, "Description": None, "Current_Status": "Pending",
    }
    monkeypatch.setattr(request_tools, "get_connection", lambda: _FakeConnection([request_row]))
    with pytest.raises(NotRaiseCapacityRequestError):
        request_tools.get_raise_capacity_request("R1")


# ----------------------------------------------------------------------------------------
# Pipeline wiring (all data sources faked)
# ----------------------------------------------------------------------------------------

def _fake_pipeline(monkeypatch, completed_hours):
    monkeypatch.setattr(agent, "get_raise_capacity_request", lambda rid: {
        "Student_ID": "S1", "Course_ID": "1501215", "Section_ID": "61", "Term": 202510,
    })
    monkeypatch.setattr(agent, "get_student_academic_info", lambda sid: {
        "Student_ID": "S1", "Student_Name": "Test Student", "Completed_Hours": completed_hours,
    })
    monkeypatch.setattr(agent, "get_student_transcript", lambda sid: _transcript(("1501211", "B")))
    monkeypatch.setattr(agent, "fetch_course", lambda cid: {"Course_ID": cid, "Course_Name": "Data Structures"})
    monkeypatch.setattr(agent, "get_section_capacity", lambda sid, cid, term: {
        "Current_Capacity": 55, "Total_Capacity": 50, "Additional_Capacity": 10,
    })


def test_pipeline_applies_completed_hours_rule_to_data_structures(monkeypatch, fake_uos_page):
    _fake_pipeline(monkeypatch, completed_hours=20)
    checks = agent._build_evidence_package("R1")["checks"]
    assert checks["prerequisite"]["satisfied"] is True
    assert checks["completed_hours"]["applicable"] is True
    assert checks["completed_hours"]["required_hours"] == 31
    assert checks["completed_hours"]["satisfied"] is False
    assert checks["capacity"]["satisfied"] is True


def test_pipeline_null_completed_hours_does_not_crash(monkeypatch, fake_uos_page):
    _fake_pipeline(monkeypatch, completed_hours=None)
    hours = agent._build_evidence_package("R1")["checks"]["completed_hours"]
    assert hours["applicable"] is True
    assert hours["satisfied"] is None


# ----------------------------------------------------------------------------------------
# Gemini failure handling (fake client -- never calls the real API)
# ----------------------------------------------------------------------------------------

class _FakeModels:
    def __init__(self, behaviour):
        self._behaviour = behaviour

    def generate_content(self, **kwargs):
        return self._behaviour()


class _FakeClient:
    def __init__(self, behaviour):
        self.models = _FakeModels(behaviour)


def _raise_api_error():
    raise RuntimeError("429 RESOURCE_EXHAUSTED")


class _Unparsed:
    parsed = None


@pytest.mark.parametrize("behaviour", [_raise_api_error, lambda: _Unparsed()])
def test_gemini_failure_becomes_manual_review(monkeypatch, behaviour):
    monkeypatch.setattr(gemini, "_client", _FakeClient(behaviour))
    decision = gemini.get_gemini_decision({"checks": {}})
    assert decision["status"] == "unavailable"
    assert decision["recommendation"] == "MANUAL_REVIEW"
    assert decision["error"]
