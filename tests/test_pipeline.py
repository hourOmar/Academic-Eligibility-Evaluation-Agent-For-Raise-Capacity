"""Automated tests for the combined pipeline (app/pipeline.py).

The real Academic Eligibility Evaluation Agent workflow runs end to end, with every data
source faked: database tools, the UoS degree page, the eligibility Gemini call, and the
separate rejection-explanation Gemini call. No .env, network, or API quota is needed.

Run with:  python -m pytest
"""

import copy

import pytest
import requests

from app import agent, pipeline
from app.services import gemini, rejection_gemini
from app.services.gemini import EligibilityDecision
from app.services.rejection_gemini import RejectionExplanations
from app.tools import prerequisite_tools

_FAKE_UOS_PAGE = """
<html><body><div id="degree-requirements"><table>
<tr><td>1501215</td><td>Data Structures</td><td>3</td><td>1501211</td></tr>
</table></div></body></html>
"""


class _PageResponse:
    text = _FAKE_UOS_PAGE

    def raise_for_status(self):
        pass


class _Response:
    def __init__(self, parsed):
        self.parsed = parsed


class _FakeModels:
    def __init__(self, behaviour):
        self._behaviour = behaviour
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        return self._behaviour()


class _FakeClient:
    def __init__(self, behaviour):
        self.models = _FakeModels(behaviour)


def _eligibility_decision(recommendation):
    parsed = EligibilityDecision.model_validate({
        "recommendation": recommendation,
        "reasoning": f"Fake Gemini decided {recommendation}.",
        "checks": {"prerequisite": "p", "completed_hours": "h", "capacity": "c"},
    })
    return lambda: _Response(parsed)


def _eligibility_gemini_down():
    raise RuntimeError("503 UNAVAILABLE")


def _rejection_wording():
    keywords = ["completed_hours_insufficient"]
    parsed = RejectionExplanations.model_validate({
        "formal": {"text": "You have completed 20 credit hours, which is below the minimum of 31 completed "
                           "credit hours applied in this evaluation.", "addresses_keywords": keywords},
        "concise": {"text": "20 completed credit hours; the minimum applied is 31.", "addresses_keywords": keywords},
        "supportive": {"text": "Thank you for your request. The minimum applied in this evaluation is 31 "
                               "completed credit hours and you currently have 20.", "addresses_keywords": keywords},
    })
    return lambda: _Response(parsed)


@pytest.fixture
def fake_sources(monkeypatch):
    """Fakes every data source; returns a function that configures one scenario."""
    monkeypatch.setattr(prerequisite_tools, "_page_cache", None)

    def configure(recommendation, completed_hours=40, transcript_grade="B", uos_reachable=True,
                  current_capacity=55):
        if uos_reachable:
            monkeypatch.setattr(prerequisite_tools.requests, "get", lambda *a, **k: _PageResponse())
        else:
            def _offline(*a, **k):
                raise requests.ConnectionError("offline")
            monkeypatch.setattr(prerequisite_tools.requests, "get", _offline)

        monkeypatch.setattr(agent, "get_raise_capacity_request", lambda rid: {
            "Student_ID": "S1", "Course_ID": "1501215", "Section_ID": "61", "Term": 202510,
        })
        monkeypatch.setattr(agent, "get_student_academic_info", lambda sid: {
            "Student_ID": "S1", "Student_Name": "Test Student", "Completed_Hours": completed_hours,
        })
        monkeypatch.setattr(agent, "get_student_transcript",
                            lambda sid: [{"Course_ID": "1501211", "Grade": transcript_grade}])
        monkeypatch.setattr(agent, "fetch_course", lambda cid: {"Course_ID": cid, "Course_Name": "Data Structures"})
        monkeypatch.setattr(agent, "get_section_capacity", lambda sid, cid, term: {
            "Current_Capacity": current_capacity, "Total_Capacity": 50, "Additional_Capacity": 10,
        })

        behaviour = _eligibility_decision(recommendation) if recommendation else _eligibility_gemini_down
        monkeypatch.setattr(gemini, "_client", _FakeClient(behaviour))
        rejection_client = _FakeClient(_rejection_wording())
        monkeypatch.setattr(rejection_gemini, "_get_client", lambda: rejection_client)
        return rejection_client

    return configure


def _forbid_rejection_agent(monkeypatch):
    def _fail(_result):
        raise AssertionError("The Rejection Explanation Agent must not run for this recommendation.")
    monkeypatch.setattr(pipeline, "explain_rejection", _fail)


# ----------------------------------------------------------------------------------------
# APPROVE / MANUAL_REVIEW skip the Rejection Explanation Agent
# ----------------------------------------------------------------------------------------

def test_approve_skips_rejection_explanation(monkeypatch, fake_sources):
    rejection_client = fake_sources("APPROVE")
    _forbid_rejection_agent(monkeypatch)
    result = pipeline.run_pipeline("R1")
    assert result["recommendation"] == "APPROVE"
    assert result["rejection_explanation"] is None
    assert result["rejection_explanation_status"] == "not_run"
    assert result["consistency_warnings"] == []
    assert result["administrative_review_required"] is False
    assert rejection_client.models.calls == 0


def test_approve_with_confirmed_failure_adds_warning_without_changing_recommendation(monkeypatch, fake_sources):
    fake_sources("APPROVE", completed_hours=20)  # completed-hours rule (31) is a confirmed failure
    _forbid_rejection_agent(monkeypatch)
    result = pipeline.run_pipeline("R1")
    assert result["recommendation"] == "APPROVE"
    assert result["academic_eligibility_evaluation"]["recommendation"] == "APPROVE"
    assert [w["type"] for w in result["consistency_warnings"]] == ["approve_with_confirmed_failure"]
    assert result["consistency_warnings"][0]["confirmed_failed_keywords"] == ["completed_hours_insufficient"]
    assert result["administrative_review_required"] is True


def test_manual_review_skips_rejection_and_returns_unresolved_evidence(monkeypatch, fake_sources):
    fake_sources("MANUAL_REVIEW", uos_reachable=False)
    _forbid_rejection_agent(monkeypatch)
    result = pipeline.run_pipeline("R1")
    assert result["recommendation"] == "MANUAL_REVIEW"
    assert result["rejection_explanation"] is None
    assert [u["check"] for u in result["unresolved_evidence"]] == ["prerequisite"]
    assert result["unresolved_evidence"][0]["issue"] == "not_verified"
    assert result["administrative_review_required"] is True


def test_eligibility_gemini_failure_is_manual_review_with_gemini_listed_as_unresolved(monkeypatch, fake_sources):
    fake_sources(None, completed_hours=20)  # eligibility Gemini raises
    _forbid_rejection_agent(monkeypatch)
    result = pipeline.run_pipeline("R1")
    assert result["recommendation"] == "MANUAL_REVIEW"
    assert "gemini_decision" in [u["check"] for u in result["unresolved_evidence"]]


# ----------------------------------------------------------------------------------------
# REJECT activates the Rejection Explanation Agent
# ----------------------------------------------------------------------------------------

def test_reject_activates_rejection_explanation(fake_sources):
    rejection_client = fake_sources("REJECT", completed_hours=20)
    result = pipeline.run_pipeline("R1")
    rejection = result["rejection_explanation"]
    assert result["recommendation"] == "REJECT"
    assert result["rejection_explanation_status"] == "generated"
    assert rejection["verified_rejection_keywords"] == ["completed_hours_insufficient"]
    assert set(rejection["explanations"]) == {"formal", "concise", "supportive"}
    assert rejection_client.models.calls == 1
    assert result["final_decision_by"] == "Authorized administrative staff"
    # 1501215's completed-hours rule is a development_rule -> flagged for confirmation.
    assert rejection["administrative_notes"]
    assert result["administrative_review_required"] is True


def test_eligibility_503_recovers_on_fallback_model_end_to_end(monkeypatch, fake_sources):
    from google.genai import errors as genai_errors
    from app import config

    fake_sources("REJECT", completed_hours=20)
    monkeypatch.setattr(config, "GEMINI_MODEL", "gemini-primary")
    monkeypatch.setattr(config, "GEMINI_FALLBACK_MODELS", ["gemini-fallback-1"])
    decide_reject = _eligibility_decision("REJECT")
    script = [genai_errors.ServerError(503, {"message": "overloaded", "status": "UNAVAILABLE"}), None]

    class _Models:
        calls = []

        def generate_content(self, model, contents, config):
            self.calls.append(model)
            behaviour = script[len(self.calls) - 1]
            if isinstance(behaviour, Exception):
                raise behaviour
            return decide_reject()

    monkeypatch.setattr(gemini, "_client", type("C", (), {"models": _Models()})())
    result = pipeline.run_pipeline("R1")
    eligibility = result["academic_eligibility_evaluation"]
    assert eligibility["gemini_status"] == "ok"
    assert eligibility["recommendation"] == "REJECT"
    assert eligibility["gemini_model_used"] == "gemini-fallback-1"
    assert [a["model"] for a in eligibility["gemini_models_attempted"]] == ["gemini-primary", "gemini-fallback-1"]
    assert result["rejection_explanation_status"] == "generated"


def test_or_prerequisite_end_to_end_does_not_require_both_courses(monkeypatch, fake_sources):
    fake_sources("REJECT", transcript_grade="F")
    or_page = _FAKE_UOS_PAGE.replace("<td>1501211</td></tr>", "<td>1501211 or 1501341</td></tr>")
    monkeypatch.setattr(prerequisite_tools, "_page_cache", None)
    monkeypatch.setattr(prerequisite_tools.requests, "get",
                        lambda *a, **k: type("R", (), {"text": or_page, "raise_for_status": lambda self: None})())
    result = pipeline.run_pipeline("R1")
    assert result["academic_eligibility_evaluation"]["evidence"]["checks"]["prerequisite"]["relationship_type"] == "OR"
    rejection = result["rejection_explanation"]
    assert rejection["verified_rejection_keywords"] == ["prerequisite_options_not_satisfied"]
    supportive = rejection["explanations"]["supportive"]["text"]
    assert "any one of" in supportive
    assert "both" not in supportive.lower()


def test_reject_with_multiple_failures_in_pipeline(fake_sources):
    fake_sources("REJECT", completed_hours=20, transcript_grade="F", current_capacity=60)
    rejection = pipeline.run_pipeline("R1")["rejection_explanation"]
    # The fake Gemini wording only covers one keyword -> validation fails -> fallback covers all.
    assert rejection["status"] == "fallback"
    assert rejection["verified_rejection_keywords"] == [
        "prerequisite_not_passed", "completed_hours_insufficient", "no_remaining_capacity",
    ]


def test_reject_without_confirmed_failure_is_flagged_not_changed(fake_sources):
    fake_sources("REJECT")  # every deterministic check passes
    result = pipeline.run_pipeline("R1")
    assert result["recommendation"] == "REJECT"
    assert result["rejection_explanation_status"] == "explanation_unavailable"
    assert [w["type"] for w in result["consistency_warnings"]] == ["reject_without_confirmed_failure"]
    assert result["administrative_review_required"] is True


# ----------------------------------------------------------------------------------------
# The eligibility result is preserved
# ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("recommendation, hours", [("APPROVE", 40), ("REJECT", 20), ("MANUAL_REVIEW", 40)])
def test_pipeline_eligibility_result_equals_standalone_agent(fake_sources, recommendation, hours):
    fake_sources(recommendation, completed_hours=hours)
    standalone = agent.evaluate_raise_capacity_request("R1")
    combined = pipeline.run_pipeline("R1")
    assert combined["academic_eligibility_evaluation"] == standalone


def test_rejection_agent_cannot_modify_pipeline_eligibility_result(monkeypatch, fake_sources):
    fake_sources("REJECT", completed_hours=20)
    seen = {}

    def _tampering_agent(eligibility):
        eligibility["recommendation"] = "APPROVE"
        eligibility["evidence"]["checks"]["completed_hours"]["satisfied"] = True
        seen["called"] = True
        return {"status": "generated", "requires_administrative_review": False}

    monkeypatch.setattr(pipeline, "explain_rejection", _tampering_agent)
    result = pipeline.run_pipeline("R1")
    assert seen["called"]
    assert result["recommendation"] == "REJECT"
    assert result["academic_eligibility_evaluation"]["recommendation"] == "REJECT"
    assert result["academic_eligibility_evaluation"]["evidence"]["checks"]["completed_hours"]["satisfied"] is False


@pytest.mark.parametrize("recommendation, hours", [("APPROVE", 20), ("REJECT", 20), ("MANUAL_REVIEW", 40)])
def test_pipeline_report_prints(fake_sources, capsys, recommendation, hours):
    fake_sources(recommendation, completed_hours=hours)
    result = pipeline.run_pipeline("R1")
    snapshot = copy.deepcopy(result)
    pipeline.print_pipeline_report(result)
    out = capsys.readouterr().out
    assert "RAISE CAPACITY ELIGIBILITY" in out
    assert "FULL JSON RESULT" not in out  # the eligibility JSON is not duplicated
    assert ("REJECTION EXPLANATION" in out) is (recommendation == "REJECT")
    assert result == snapshot
