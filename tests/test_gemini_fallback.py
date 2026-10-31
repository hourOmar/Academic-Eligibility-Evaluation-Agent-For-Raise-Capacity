"""Automated tests for the Gemini model-fallback mechanism (same approach as the Raise
Capacity Prioritization Agent): try [GEMINI_MODEL, *GEMINI_FALLBACK_MODELS] in order,
moving on ONLY on a retryable 429/503 error.

Covers both the Eligibility Agent call (app/services/gemini.py) and the Rejection
Explanation Agent call (app/services/rejection_gemini.py). Uses the real
google.genai.errors types and a scripted fake client -- no real Gemini API call.

Run with:  python -m pytest
"""

import pytest
from google.genai import errors as genai_errors

from app import config, rejection_agent
from app.services import gemini, rejection_gemini
from app.services.gemini import EligibilityDecision
from app.services.rejection_gemini import RejectionExplanations

# ----------------------------------------------------------------------------------------
# Scripted fake client
# ----------------------------------------------------------------------------------------


class _Response:
    def __init__(self, parsed):
        self.parsed = parsed


class _ScriptedModels:
    """Consumes one scripted behaviour per call: an Exception to raise, or a parsed object."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if len(self.calls) > len(self._script):
            raise AssertionError(f"generate_content called {len(self.calls)} times; only "
                                 f"{len(self._script)} scripted response(s).")
        behaviour = self._script[len(self.calls) - 1]
        if isinstance(behaviour, Exception):
            raise behaviour
        return _Response(behaviour)


class _ScriptedClient:
    def __init__(self, script):
        self.models = _ScriptedModels(script)


def _server_error(message="The model is overloaded. Please try again later."):
    return genai_errors.ServerError(503, {"message": message, "status": "UNAVAILABLE"})


def _rate_limit_error():
    return genai_errors.ClientError(429, {"message": "Resource exhausted.", "status": "RESOURCE_EXHAUSTED"})


def _bad_request_error():
    return genai_errors.ClientError(400, {"message": "Invalid argument.", "status": "INVALID_ARGUMENT"})


def _decision(recommendation="REJECT"):
    return EligibilityDecision.model_validate({
        "recommendation": recommendation,
        "reasoning": f"Decided {recommendation}.",
        "checks": {"prerequisite": "p", "completed_hours": "h", "capacity": "c"},
    })


@pytest.fixture
def models(monkeypatch):
    def _set(primary="gemini-primary", fallbacks=("gemini-fallback-1", "gemini-fallback-2")):
        monkeypatch.setattr(config, "GEMINI_MODEL", primary)
        monkeypatch.setattr(config, "GEMINI_FALLBACK_MODELS", list(fallbacks))
    _set()
    return _set


def _eligibility(monkeypatch, script):
    client = _ScriptedClient(script)
    monkeypatch.setattr(gemini, "_client", client)
    return gemini.get_gemini_decision({"checks": {}}), client


def _called_models(client):
    return [c["model"] for c in client.models.calls]


# ----------------------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------------------

def test_fallback_list_is_parsed_like_the_prioritization_agent(monkeypatch):
    import importlib
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", " gemini-3.6-flash, ,gemini-3.7-flash,gemini-3.5-flash ")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.GEMINI_FALLBACK_MODELS == ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.5-flash"]
    finally:
        monkeypatch.delenv("GEMINI_FALLBACK_MODELS")
        importlib.reload(config)


def test_duplicate_models_are_tried_once(models):
    models(primary="gemini-a", fallbacks=["gemini-a", "gemini-b", "gemini-b"])
    assert gemini.models_to_try() == ["gemini-a", "gemini-b"]


# ----------------------------------------------------------------------------------------
# Eligibility Agent
# ----------------------------------------------------------------------------------------

def test_primary_success_does_not_call_fallback(monkeypatch, models):
    decision, client = _eligibility(monkeypatch, [_decision("APPROVE")])
    assert decision["status"] == "ok"
    assert decision["recommendation"] == "APPROVE"
    assert _called_models(client) == ["gemini-primary"]
    assert decision["model_used"] == "gemini-primary"
    assert decision["models_attempted"] == [{"model": "gemini-primary", "error": None}]


@pytest.mark.parametrize("make_error", [_server_error, _rate_limit_error])
def test_primary_retryable_failure_falls_back_to_next_model(monkeypatch, models, make_error):
    decision, client = _eligibility(monkeypatch, [make_error(), _decision("REJECT")])
    assert decision["status"] == "ok"
    assert decision["recommendation"] == "REJECT"  # the fallback model's decision, unchanged
    assert _called_models(client) == ["gemini-primary", "gemini-fallback-1"]
    assert decision["model_used"] == "gemini-fallback-1"
    assert decision["models_attempted"][0]["model"] == "gemini-primary"
    assert decision["models_attempted"][0]["error"]
    assert decision["models_attempted"][1] == {"model": "gemini-fallback-1", "error": None}


def test_retryable_failures_continue_through_the_whole_list(monkeypatch, models):
    decision, client = _eligibility(monkeypatch, [_server_error(), _rate_limit_error(), _decision("APPROVE")])
    assert decision["recommendation"] == "APPROVE"
    assert _called_models(client) == ["gemini-primary", "gemini-fallback-1", "gemini-fallback-2"]
    assert decision["model_used"] == "gemini-fallback-2"


def test_fallback_sends_the_identical_request(monkeypatch, models):
    _, client = _eligibility(monkeypatch, [_server_error(), _decision()])
    first, second = client.models.calls
    assert first["contents"] == second["contents"]
    assert first["config"] is second["config"]
    assert first["config"].response_schema is EligibilityDecision


def test_all_models_failing_is_manual_review_naming_every_model(monkeypatch, models):
    decision, client = _eligibility(
        monkeypatch, [_server_error(), _rate_limit_error(), _server_error("Still unavailable.")]
    )
    assert len(client.models.calls) == 3
    assert decision["status"] == "unavailable"
    assert decision["recommendation"] == "MANUAL_REVIEW"
    assert decision["model_used"] is None
    assert [a["model"] for a in decision["models_attempted"]] == [
        "gemini-primary", "gemini-fallback-1", "gemini-fallback-2",
    ]
    assert all(a["error"] for a in decision["models_attempted"])
    for name in ("gemini-primary", "gemini-fallback-1", "gemini-fallback-2"):
        assert name in decision["error"]


@pytest.mark.parametrize("error", [_bad_request_error(), ConnectionError("simulated network failure")])
def test_non_retryable_error_stops_immediately(monkeypatch, models, error):
    decision, client = _eligibility(monkeypatch, [error])
    assert _called_models(client) == ["gemini-primary"]
    assert decision["recommendation"] == "MANUAL_REVIEW"
    assert len(decision["models_attempted"]) == 1


def test_no_fallback_configured_behaves_as_before(monkeypatch, models):
    models(primary="gemini-primary", fallbacks=[])
    decision, client = _eligibility(monkeypatch, [_server_error()])
    assert _called_models(client) == ["gemini-primary"]
    assert decision["recommendation"] == "MANUAL_REVIEW"


def test_unparseable_response_is_not_a_fallback_trigger(monkeypatch, models):
    decision, client = _eligibility(monkeypatch, [None])
    assert _called_models(client) == ["gemini-primary"]
    assert decision["recommendation"] == "MANUAL_REVIEW"
    assert decision["models_attempted"] == [{"model": "gemini-primary", "error": None}]


def test_missing_api_key_reports_no_attempts(monkeypatch, models):
    monkeypatch.setattr(gemini, "_client", None)
    monkeypatch.setattr(config, "GEMINI_API_KEY", None)
    decision = gemini.get_gemini_decision({"checks": {}})
    assert decision["recommendation"] == "MANUAL_REVIEW"
    assert decision["models_attempted"] == []


# ----------------------------------------------------------------------------------------
# Rejection Explanation Agent
# ----------------------------------------------------------------------------------------

def _hours_reject_result():
    course = {"Course_ID": "1501215", "Course_Name": "Data Structures"}
    return {
        "request_id": "R1", "course": course,
        "evidence": {
            "student": {"Student_ID": "U1", "Student_Name": "Test Student"}, "course": course, "section_id": "61",
            "checks": {
                "prerequisite": {"verified": True, "satisfied": True},
                "completed_hours": {"applicable": True, "satisfied": False,
                                    "student_completed_hours": 20, "required_hours": 31},
                "capacity": {"satisfied": True},
            },
        },
        "recommendation": "REJECT", "reasoning": "r", "checks": {}, "gemini_status": "ok", "gemini_error": None,
    }


def _valid_wording():
    kw = ["completed_hours_insufficient"]
    return RejectionExplanations.model_validate({
        "formal": {"text": "You have completed 20 credit hours, below the minimum of 31 applied in this "
                           "evaluation.", "addresses_keywords": kw},
        "concise": {"text": "20 completed credit hours; the minimum applied is 31.", "addresses_keywords": kw},
        "supportive": {"text": "Thank you for your request. At least 31 completed credit hours would be "
                               "needed under the criterion applied in this evaluation.", "addresses_keywords": kw},
    })


def _rejection(monkeypatch, script):
    client = _ScriptedClient(script)
    monkeypatch.setattr(rejection_gemini, "_get_client", lambda: client)
    return rejection_agent.explain_rejection(_hours_reject_result()), client


def test_rejection_primary_503_falls_back_and_generates(monkeypatch, models):
    result, client = _rejection(monkeypatch, [_server_error(), _valid_wording()])
    assert result["status"] == "generated"
    assert _called_models(client) == ["gemini-primary", "gemini-fallback-1"]
    assert result["gemini_model_used"] == "gemini-fallback-1"
    assert [a["model"] for a in result["gemini_models_attempted"]] == ["gemini-primary", "gemini-fallback-1"]


def test_rejection_all_models_failing_uses_deterministic_fallback(monkeypatch, models):
    result, client = _rejection(monkeypatch, [_server_error(), _rate_limit_error(), _server_error()])
    assert len(client.models.calls) == 3
    assert result["status"] == "fallback"
    assert result["explanation_source"] == "deterministic_fallback"
    assert result["gemini_status"] == "unavailable"
    assert result["gemini_model_used"] is None
    assert len(result["gemini_models_attempted"]) == 3
    assert "gemini-fallback-2" in result["gemini_error"]
    assert set(result["explanations"]) == {"formal", "concise", "supportive"}
    assert result["eligibility_recommendation"] == "REJECT"


def test_rejection_non_retryable_error_uses_deterministic_fallback_after_one_call(monkeypatch, models):
    result, client = _rejection(monkeypatch, [_bad_request_error()])
    assert _called_models(client) == ["gemini-primary"]
    assert result["status"] == "fallback"


def test_rejection_invalid_wording_from_fallback_model_still_fails_validation(monkeypatch, models):
    bad = RejectionExplanations.model_validate({
        tone: {"text": "Your request has been rejected.", "addresses_keywords": ["completed_hours_insufficient"]}
        for tone in ("formal", "concise", "supportive")
    })
    result, _ = _rejection(monkeypatch, [_server_error(), bad])
    assert result["status"] == "fallback"
    assert result["gemini_status"] == "invalid_output"
    assert result["gemini_model_used"] == "gemini-fallback-1"
