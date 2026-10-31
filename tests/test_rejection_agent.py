"""Automated tests for the Rejection Explanation Agent.

The agent's only input is an Academic Eligibility Evaluation Agent result, so these tests
build synthetic results directly. Gemini is replaced with a fake client -- no API key,
database, or network access is needed.

Run with:  python -m pytest
"""

import copy

import pytest

from app import rejection_agent
from app.exceptions import InvalidEligibilityResultError
from app.services import rejection_gemini
from app.services.rejection_gemini import RejectionExplanations
from app.tools import rejection_keyword_tools
from app.tools.rejection_input_tools import parse_eligibility_text

# ----------------------------------------------------------------------------------------
# Synthetic eligibility results
# ----------------------------------------------------------------------------------------

PREREQ_SATISFIED = {
    "raw_prerequisite_text": "1501211", "relationship_type": "SINGLE",
    "required_prerequisite_groups": [[1501211]], "never_attempted": [],
    "failed_or_incomplete_courses": [], "unresolved_grade_courses": [],
    "satisfied": True, "verified": True, "source_url": "https://www.sharjah.ac.ae/x", "details": "ok",
}
PREREQ_NEVER_ATTEMPTED = {**PREREQ_SATISFIED, "never_attempted": [1501211], "satisfied": False}
PREREQ_NOT_PASSED = {**PREREQ_SATISFIED, "failed_or_incomplete_courses": [1501211], "satisfied": False}
PREREQ_UNVERIFIED = {
    **PREREQ_SATISFIED, "relationship_type": None, "required_prerequisite_groups": [],
    "satisfied": None, "verified": False, "details": "Could not reach the UoS page.",
}
PREREQ_UNRESOLVED_GRADE = {**PREREQ_SATISFIED, "unresolved_grade_courses": [1501211], "satisfied": None}

HOURS_NOT_APPLICABLE = {"applicable": False, "requirement_found": False, "satisfied": None,
                        "student_completed_hours": 20, "required_hours": None}
HOURS_SATISFIED = {"applicable": True, "requirement_found": True, "satisfied": True,
                   "student_completed_hours": 40, "required_hours": 31}
HOURS_INSUFFICIENT = {**HOURS_SATISFIED, "satisfied": False, "student_completed_hours": 20}
HOURS_MISSING = {**HOURS_SATISFIED, "satisfied": None, "student_completed_hours": None, "error": "missing"}

CAPACITY_AVAILABLE = {"current_capacity": 55, "total_capacity": 50, "additional_capacity": 10,
                      "maximum_allowed_capacity": 60, "remaining_raise_capacity": 5, "satisfied": True}
CAPACITY_FULL = {**CAPACITY_AVAILABLE, "current_capacity": 60, "remaining_raise_capacity": 0, "satisfied": False}
CAPACITY_INVALID = {**CAPACITY_AVAILABLE, "maximum_allowed_capacity": None, "remaining_raise_capacity": None,
                    "satisfied": None, "error": "invalid"}


def make_result(recommendation, prerequisite=PREREQ_SATISFIED, hours=HOURS_SATISFIED, capacity=CAPACITY_AVAILABLE):
    course = {"Course_ID": "1501215", "Course_Name": "Data Structures"}
    return {
        "request_id": "STUD_RSCA_000001",
        "course": course,
        "evidence": {
            "request_id": "STUD_RSCA_000001",
            "student": {"Student_ID": "U21100001", "Student_Name": "Test Student"},
            "course": course,
            "section_id": "61",
            "checks": {"prerequisite": prerequisite, "completed_hours": hours, "capacity": capacity},
        },
        "recommendation": recommendation,
        "reasoning": "Gemini reasoning.",
        "checks": {"prerequisite": "p", "completed_hours": "h", "capacity": "c"},
        "gemini_status": "ok",
        "gemini_error": None,
    }


# ----------------------------------------------------------------------------------------
# Fake Gemini (rejection wording call)
# ----------------------------------------------------------------------------------------

class _Response:
    def __init__(self, parsed):
        self.parsed = parsed


class _FakeModels:
    def __init__(self, behaviour):
        self._behaviour = behaviour
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self._behaviour()


class _FakeClient:
    def __init__(self, behaviour):
        self.models = _FakeModels(behaviour)


def use_fake_gemini(monkeypatch, behaviour) -> _FakeClient:
    client = _FakeClient(behaviour)
    monkeypatch.setattr(rejection_gemini, "_get_client", lambda: client)
    return client


def gemini_returns(formal, concise, supportive, keywords):
    parsed = RejectionExplanations.model_validate({
        "formal": {"text": formal, "addresses_keywords": keywords},
        "concise": {"text": concise, "addresses_keywords": keywords},
        "supportive": {"text": supportive, "addresses_keywords": keywords},
    })
    return lambda: _Response(parsed)


def _raise_api_error():
    raise RuntimeError("429 RESOURCE_EXHAUSTED")


# Course 1501215's completed-hours rule is a development_rule in course_completed_hours.json,
# so valid wording describes it as the minimum applied in this evaluation.
VALID_HOURS_EXPLANATIONS = (
    "The request does not meet the completed credit hours condition applied in this evaluation: you "
    "have completed 20 credit hours, which is below the minimum of 31 completed credit hours.",
    "Completed credit hours (20) are below the minimum of 31 applied in this evaluation.",
    "Thank you for your request. You currently have 20 completed credit hours, below the minimum of "
    "31 applied in this evaluation. Under this criterion, at least 31 completed credit hours would be needed.",
)

PREREQ_OR_FAILED = {
    **PREREQ_SATISFIED, "raw_prerequisite_text": "1501211 or 1501341", "relationship_type": "OR",
    "required_prerequisite_groups": [[1501211], [1501341]],
    "never_attempted": [1501341], "failed_or_incomplete_courses": [1501211], "satisfied": False,
}
PREREQ_AND_PARTIAL = {
    **PREREQ_SATISFIED, "raw_prerequisite_text": "1501215, 1501279", "relationship_type": "AND",
    "required_prerequisite_groups": [[1501215, 1501279]],
    "never_attempted": [1501279], "failed_or_incomplete_courses": [], "satisfied": False,
}


# ----------------------------------------------------------------------------------------
# Routing: only REJECT activates explanation generation
# ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("recommendation", ["APPROVE", "MANUAL_REVIEW"])
def test_non_reject_is_not_applicable_and_never_calls_gemini(monkeypatch, recommendation):
    client = use_fake_gemini(monkeypatch, gemini_returns(*VALID_HOURS_EXPLANATIONS, ["completed_hours_insufficient"]))
    result = rejection_agent.explain_rejection(make_result(recommendation, hours=HOURS_INSUFFICIENT))
    assert result["status"] == "not_applicable"
    assert result["explanations"] is None
    assert result["verified_rejection_keywords"] == []
    assert client.models.calls == []


def test_reject_with_valid_gemini_output_is_generated(monkeypatch):
    client = use_fake_gemini(monkeypatch, gemini_returns(*VALID_HOURS_EXPLANATIONS, ["completed_hours_insufficient"]))
    result = rejection_agent.explain_rejection(make_result("REJECT", hours=HOURS_INSUFFICIENT))
    assert result["status"] == "generated"
    assert result["explanation_source"] == "gemini"
    assert result["verified_rejection_keywords"] == ["completed_hours_insufficient"]
    assert set(result["explanations"]) == {"formal", "concise", "supportive"}
    assert result["explanations"]["formal"]["text"] == VALID_HOURS_EXPLANATIONS[0]
    assert len(client.models.calls) == 1


def test_gemini_context_excludes_student_personal_information(monkeypatch):
    client = use_fake_gemini(monkeypatch, gemini_returns(*VALID_HOURS_EXPLANATIONS, ["completed_hours_insufficient"]))
    rejection_agent.explain_rejection(make_result("REJECT", hours=HOURS_INSUFFICIENT))
    sent = client.models.calls[0]["contents"]
    assert "Test Student" not in sent
    assert "U21100001" not in sent


# ----------------------------------------------------------------------------------------
# Confirmed failures -> keywords
# ----------------------------------------------------------------------------------------

def test_multiple_failed_requirements_are_all_represented(monkeypatch):
    use_fake_gemini(monkeypatch, _raise_api_error)
    result = rejection_agent.explain_rejection(
        make_result("REJECT", prerequisite=PREREQ_OR_FAILED, hours=HOURS_INSUFFICIENT, capacity=CAPACITY_FULL)
    )
    assert result["verified_rejection_keywords"] == [
        "prerequisite_options_not_satisfied", "completed_hours_insufficient", "no_remaining_capacity",
    ]
    assert {f["requirement"] for f in result["confirmed_failed_requirements"]} == {
        "prerequisite", "completed_hours", "capacity",
    }
    for tone in ("formal", "concise", "supportive"):
        explanation = result["explanations"][tone]
        assert set(explanation["addresses_keywords"]) == set(result["verified_rejection_keywords"])
        for fact in ("1501341", "1501211", "31", "60"):
            assert fact in explanation["text"]


def test_never_attempted_and_not_passed_prerequisites_map_to_distinct_keywords():
    kb = rejection_agent.load_rejection_knowledge_base()
    never = rejection_agent.identify_confirmed_failures(make_result("REJECT", PREREQ_NEVER_ATTEMPTED)["evidence"], kb)
    failed = rejection_agent.identify_confirmed_failures(make_result("REJECT", PREREQ_NOT_PASSED)["evidence"], kb)
    assert [f["keyword"] for f in never] == ["prerequisite_not_completed"]
    assert [f["keyword"] for f in failed] == ["prerequisite_not_passed"]


# ----------------------------------------------------------------------------------------
# AND vs OR prerequisite relationships
# ----------------------------------------------------------------------------------------

def test_or_prerequisite_is_one_reason_that_requires_only_one_option(monkeypatch):
    use_fake_gemini(monkeypatch, _raise_api_error)
    result = rejection_agent.explain_rejection(make_result("REJECT", prerequisite=PREREQ_OR_FAILED))
    assert result["verified_rejection_keywords"] == ["prerequisite_options_not_satisfied"]
    failure = result["confirmed_failed_requirements"][0]
    assert "any one of the following options: 1501211 or 1501341" in failure["plain_reason"]
    assert "any one of these prerequisite options" in failure["supportive_guidance"]
    for tone in ("formal", "concise", "supportive"):
        text = result["explanations"][tone]["text"]
        assert "1501211" in text and "1501341" in text
        lowered = text.lower()
        for wording in ("both", "all of", "each of", "need to be completed", "required prerequisite course"):
            assert wording not in lowered, (tone, wording)


def test_or_prerequisite_with_and_group_keeps_grouping():
    kb = rejection_agent.load_rejection_knowledge_base()
    prereq = {**PREREQ_OR_FAILED, "required_prerequisite_groups": [[1501215, 1501279], [1501341]],
              "never_attempted": [1501279, 1501341], "failed_or_incomplete_courses": []}
    failures = rejection_agent.identify_confirmed_failures(make_result("REJECT", prereq)["evidence"], kb)
    assert [f["keyword"] for f in failures] == ["prerequisite_options_not_satisfied"]
    assert "(1501215 and 1501279) or 1501341" in failures[0]["plain_reason"]


def test_and_prerequisite_names_only_the_missing_course_as_required(monkeypatch):
    use_fake_gemini(monkeypatch, _raise_api_error)
    result = rejection_agent.explain_rejection(make_result("REJECT", prerequisite=PREREQ_AND_PARTIAL))
    assert result["verified_rejection_keywords"] == ["prerequisite_not_completed"]
    failure = result["confirmed_failed_requirements"][0]
    assert "required prerequisite course(s) 1501279" in failure["plain_reason"]
    assert "1501279 need to be completed" in failure["supportive_guidance"]
    for tone in ("formal", "concise", "supportive"):
        assert "1501279" in result["explanations"][tone]["text"]


@pytest.mark.parametrize("prerequisite, expected", [
    (PREREQ_NEVER_ATTEMPTED, ["prerequisite_not_completed"]),  # SINGLE
    (PREREQ_AND_PARTIAL, ["prerequisite_not_completed"]),      # AND
    (PREREQ_OR_FAILED, ["prerequisite_options_not_satisfied"]),  # OR
])
def test_prerequisite_keyword_follows_relationship_type(prerequisite, expected):
    kb = rejection_agent.load_rejection_knowledge_base()
    failures = rejection_agent.identify_confirmed_failures(make_result("REJECT", prerequisite)["evidence"], kb)
    assert [f["keyword"] for f in failures] == expected


OR_KW = ["prerequisite_options_not_satisfied"]
OR_FORMAL = ("The prerequisite for this course can be satisfied by any one of the options 1501211 or "
             "1501341, and none of them has been completed with a passing grade.")
OR_CONCISE = "No prerequisite option (1501211 or 1501341) has been completed with a passing grade."
OR_SUPPORTIVE = ("Thank you for your request. Completing any one of the prerequisite options, 1501211 or "
                 "1501341, with a passing grade would satisfy this requirement.")


def test_valid_or_wording_from_gemini_is_accepted(monkeypatch):
    use_fake_gemini(monkeypatch, gemini_returns(OR_FORMAL, OR_CONCISE, OR_SUPPORTIVE, OR_KW))
    result = rejection_agent.explain_rejection(make_result("REJECT", prerequisite=PREREQ_OR_FAILED))
    assert result["status"] == "generated", result["validation_errors"]


@pytest.mark.parametrize("bad_supportive", [
    "You need to complete both 1501211 and 1501341 with a passing grade.",
    "All of the prerequisite courses 1501211 and 1501341 must be passed.",
    "Each of 1501211 and 1501341 must be passed before you register.",
])
def test_gemini_wording_requiring_every_or_option_is_rejected(monkeypatch, bad_supportive):
    use_fake_gemini(monkeypatch, gemini_returns(OR_FORMAL, OR_CONCISE, bad_supportive, OR_KW))
    result = rejection_agent.explain_rejection(make_result("REJECT", prerequisite=PREREQ_OR_FAILED))
    assert result["status"] == "fallback"
    assert any("implies every OR prerequisite option is required" in e for e in result["validation_errors"])


# ----------------------------------------------------------------------------------------
# Development-only completed-hours rules are not presented as official policy
# ----------------------------------------------------------------------------------------

def test_development_rule_is_not_presented_as_official_policy(monkeypatch):
    use_fake_gemini(monkeypatch, _raise_api_error)
    result = rejection_agent.explain_rejection(make_result("REJECT", hours=HOURS_INSUFFICIENT))
    failure = result["confirmed_failed_requirements"][0]
    assert failure["evidence"]["rule_status"] == "development_rule"
    assert failure["requires_policy_confirmation"] is True
    assert "not been confirmed as official University of Sharjah policy" in failure["policy_basis"]
    for tone in ("formal", "concise", "supportive"):
        text = result["explanations"][tone]["text"].lower()
        assert "applied" in text
        assert "this course requires" not in text
        assert "policy" not in text
    assert result["requires_administrative_review"] is True
    assert result["administrative_notes"] == [rejection_agent.DEVELOPMENT_RULE_NOTE]


@pytest.mark.parametrize("bad_formal", [
    "Under university policy, at least 31 completed credit hours are needed; you have completed 20.",
    "This course requires at least 31 completed credit hours, and you have completed 20.",
    "The university requires at least 31 completed credit hours; you have completed 20.",
])
def test_gemini_presenting_development_rule_as_policy_is_rejected(monkeypatch, bad_formal):
    use_fake_gemini(monkeypatch, gemini_returns(bad_formal, VALID_HOURS_EXPLANATIONS[1],
                                                VALID_HOURS_EXPLANATIONS[2], ["completed_hours_insufficient"]))
    result = rejection_agent.explain_rejection(make_result("REJECT", hours=HOURS_INSUFFICIENT))
    assert result["status"] == "fallback"
    assert any("presents a development rule as official policy" in e for e in result["validation_errors"])


@pytest.mark.parametrize("status", [None, "draft", "development_rule"])
def test_any_non_approved_rule_status_uses_development_wording(monkeypatch, status):
    monkeypatch.setattr(rejection_keyword_tools, "get_completed_hours_requirement",
                        lambda cid: {"found": True, "status": status})
    kb = rejection_agent.load_rejection_knowledge_base()
    failure = rejection_agent.identify_confirmed_failures(make_result("REJECT", hours=HOURS_INSUFFICIENT)["evidence"], kb)[0]
    assert failure["requires_policy_confirmation"] is True
    assert "applied for this course in this evaluation" in failure["plain_reason"]


def test_approved_rule_uses_confirmed_wording(monkeypatch):
    monkeypatch.setattr(rejection_keyword_tools, "get_completed_hours_requirement",
                        lambda cid: {"found": True, "status": "approved"})
    use_fake_gemini(monkeypatch, gemini_returns(
        "This course requires at least 31 completed credit hours, and you have completed 20.",
        *VALID_HOURS_EXPLANATIONS[1:], ["completed_hours_insufficient"]))
    result = rejection_agent.explain_rejection(make_result("REJECT", hours=HOURS_INSUFFICIENT))
    failure = result["confirmed_failed_requirements"][0]
    assert failure["requires_policy_confirmation"] is False
    assert "this course requires at least 31" in failure["plain_reason"]
    assert result["status"] == "generated"
    assert result["requires_administrative_review"] is False
    assert result["administrative_notes"] == []


def test_satisfied_and_not_applicable_checks_produce_no_keywords():
    kb = rejection_agent.load_rejection_knowledge_base()
    evidence = make_result("REJECT", hours=HOURS_NOT_APPLICABLE)["evidence"]
    assert rejection_agent.identify_confirmed_failures(evidence, kb) == []


# ----------------------------------------------------------------------------------------
# Missing / conflicting evidence is never a confirmed failure
# ----------------------------------------------------------------------------------------

def test_unresolved_evidence_is_excluded_from_rejection_reasons(monkeypatch):
    use_fake_gemini(monkeypatch, _raise_api_error)
    result = rejection_agent.explain_rejection(
        make_result("REJECT", prerequisite=PREREQ_UNVERIFIED, hours=HOURS_MISSING, capacity=CAPACITY_FULL)
    )
    assert result["verified_rejection_keywords"] == ["no_remaining_capacity"]
    assert {u["check"] for u in result["unresolved_checks_not_used_as_reasons"]} == {"prerequisite", "completed_hours"}
    for tone in ("formal", "concise", "supportive"):
        text = result["explanations"][tone]["text"].lower()
        assert "prerequisite" not in text
        assert "credit hour" not in text


def test_unverified_prerequisite_is_never_a_failure_even_if_satisfied_is_false():
    kb = rejection_agent.load_rejection_knowledge_base()
    conflicting = {**PREREQ_UNVERIFIED, "satisfied": False, "never_attempted": [1501211]}
    assert rejection_agent.identify_confirmed_failures(make_result("REJECT", conflicting)["evidence"], kb) == []


@pytest.mark.parametrize("prerequisite, hours, capacity", [
    (PREREQ_UNVERIFIED, HOURS_SATISFIED, CAPACITY_AVAILABLE),
    (PREREQ_UNRESOLVED_GRADE, HOURS_MISSING, CAPACITY_INVALID),
    (PREREQ_SATISFIED, HOURS_SATISFIED, CAPACITY_AVAILABLE),
])
def test_reject_without_confirmed_failure_is_explanation_unavailable(monkeypatch, prerequisite, hours, capacity):
    client = use_fake_gemini(monkeypatch, gemini_returns(*VALID_HOURS_EXPLANATIONS, ["completed_hours_insufficient"]))
    result = rejection_agent.explain_rejection(make_result("REJECT", prerequisite, hours, capacity))
    assert result["status"] == "explanation_unavailable"
    assert result["requires_administrative_review"] is True
    assert result["explanations"] is None
    assert result["eligibility_recommendation"] == "REJECT"
    assert client.models.calls == []


# ----------------------------------------------------------------------------------------
# Validation of Gemini output (all-or-nothing fallback)
# ----------------------------------------------------------------------------------------

F, C, S = VALID_HOURS_EXPLANATIONS
KW = ["completed_hours_insufficient"]


@pytest.mark.parametrize("formal, concise, supportive, keywords, expected_error", [
    (F, C, S, KW + ["low_cgpa"], "unsupported keyword"),
    (F, C, S, [], "does not address verified keyword"),
    (F, "You have 20 hours but need 45.", S, KW, "number(s) not in the verified evidence"),
    (F, C, S + " The section also has no remaining capacity.", KW, "discusses 'capacity'"),
    (F + " The prerequisite 1501211 is also missing.", C, S, KW, "discusses 'prerequisite'"),
    ("Your request has been rejected. " + F, C, S, KW, "disallowed wording"),
    (F, C, S + " This was generated by Gemini.", KW, "disallowed wording"),
    (F, "Test Student has fewer than 31 completed credit hours.", S, KW, "personal information"),
    (F, "   ", S, KW, "empty text"),
    (F, " ".join(["word"] * 61), S, KW, "longer than"),
])
def test_invalid_gemini_output_is_rejected_and_falls_back(monkeypatch, formal, concise, supportive, keywords,
                                                          expected_error):
    use_fake_gemini(monkeypatch, gemini_returns(formal, concise, supportive, keywords))
    result = rejection_agent.explain_rejection(make_result("REJECT", hours=HOURS_INSUFFICIENT))
    assert result["status"] == "fallback"
    assert result["explanation_source"] == "deterministic_fallback"
    assert result["gemini_status"] == "invalid_output"
    assert any(expected_error in e for e in result["validation_errors"])
    # All-or-nothing: none of Gemini's three texts survive, even the valid ones.
    for tone in ("formal", "concise", "supportive"):
        assert result["explanations"][tone]["text"] not in (formal, concise, supportive)


class _Unparsed:
    parsed = None


@pytest.mark.parametrize("behaviour", [_raise_api_error, lambda: _Unparsed()])
def test_gemini_failure_triggers_deterministic_fallback(monkeypatch, behaviour):
    use_fake_gemini(monkeypatch, behaviour)
    result = rejection_agent.explain_rejection(make_result("REJECT", hours=HOURS_INSUFFICIENT))
    assert result["status"] == "fallback"
    assert result["gemini_status"] == "unavailable"
    assert result["gemini_error"]
    texts = {tone: result["explanations"][tone]["text"] for tone in ("formal", "concise", "supportive")}
    assert all("20" in t and "31" in t for t in texts.values())
    assert "at least 31 completed credit hours would be needed" in texts["supportive"].lower()


def test_missing_api_key_triggers_fallback(monkeypatch):
    def _no_key():
        from app.exceptions import GeminiUnavailableError
        raise GeminiUnavailableError("GEMINI_API_KEY is not configured.")

    monkeypatch.setattr(rejection_gemini, "_get_client", _no_key)
    result = rejection_agent.explain_rejection(make_result("REJECT", capacity=CAPACITY_FULL))
    assert result["status"] == "fallback"
    assert result["verified_rejection_keywords"] == ["no_remaining_capacity"]


def test_fallback_explanations_never_claim_an_official_rejection(monkeypatch):
    use_fake_gemini(monkeypatch, _raise_api_error)
    result = rejection_agent.explain_rejection(
        make_result("REJECT", prerequisite=PREREQ_NOT_PASSED, hours=HOURS_INSUFFICIENT, capacity=CAPACITY_FULL)
    )
    for tone in ("formal", "concise", "supportive"):
        text = result["explanations"][tone]["text"].lower()
        assert "rejected" not in text
        assert "test student" not in text


# ----------------------------------------------------------------------------------------
# The agent cannot modify the eligibility recommendation
# ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("recommendation", ["APPROVE", "REJECT", "MANUAL_REVIEW"])
def test_eligibility_result_is_never_mutated(monkeypatch, recommendation):
    use_fake_gemini(monkeypatch, gemini_returns(*VALID_HOURS_EXPLANATIONS, ["completed_hours_insufficient"]))
    eligibility = make_result(recommendation, hours=HOURS_INSUFFICIENT)
    snapshot = copy.deepcopy(eligibility)
    result = rejection_agent.explain_rejection(eligibility)
    assert eligibility == snapshot
    assert result["eligibility_recommendation"] == recommendation
    assert "recommendation" not in result  # the agent has no field that could override it


# ----------------------------------------------------------------------------------------
# Input handling
# ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    None, [], {"recommendation": "MAYBE", "evidence": {"checks": {}}},
    {"recommendation": "REJECT"},
    {"recommendation": "REJECT", "evidence": {"checks": {"prerequisite": {}, "completed_hours": {}}}},
])
def test_invalid_eligibility_result_is_rejected(bad):
    with pytest.raises(InvalidEligibilityResultError):
        rejection_agent.explain_rejection(bad)


def test_parses_saved_terminal_output_of_eligibility_agent():
    text = 'RAISE CAPACITY ELIGIBILITY\n{not json}\nFULL JSON RESULT\n----\n{"recommendation": "REJECT"}\n'
    assert parse_eligibility_text(text) == {"recommendation": "REJECT"}
