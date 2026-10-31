"""Rejection Explanation Agent for Raise Capacity.

Input : the finished result of the Academic Eligibility Evaluation Agent
        (app/agent.py:evaluate_raise_capacity_request), or a saved copy of it.
Steps : validate input
        -> only for REJECT: identify CONFIRMED failed requirements (deterministic)
        -> map them to keywords from app/knowledge_base/rejection_keywords.json
        -> separate Gemini call writes formal / concise / supportive suggestions
        -> validate all three; deterministic fallback if Gemini fails or any is invalid

This agent NEVER changes the eligibility recommendation. It works on a deep copy of its
input and returns its own, separate result. The explanations are suggestions for
authorized administrative staff, who make the final decision.

Run with:  python -m app.rejection_agent <request_id | path-to-saved-eligibility-output>
"""

import copy
import json
import os
import sys
import textwrap

from app.exceptions import AgentError
from app.tools.explanation_generator import TONES, generate_rejection_explanations
from app.tools.rejection_input_tools import load_eligibility_result, validate_eligibility_result
from app.tools.rejection_keyword_tools import (
    identify_confirmed_failures,
    identify_unresolved_checks,
    load_rejection_knowledge_base,
)

NOTICE = (
    "Suggested explanations only. The final approval or rejection decision rests with "
    "authorized administrative staff."
)
DEVELOPMENT_RULE_NOTE = (
    "The completed-credit-hour threshold used in this explanation is a development rule in "
    "the local knowledge base and has not been confirmed as official University of Sharjah "
    "policy. Confirm it before using this explanation."
)
_WIDTH = 70


def _base_result(eligibility_result: dict) -> dict:
    return {
        "request_id": eligibility_result.get("request_id"),
        "eligibility_recommendation": eligibility_result["recommendation"],
        "status": None,
        "verified_rejection_keywords": [],
        "confirmed_failed_requirements": [],
        "unresolved_checks_not_used_as_reasons": [],
        "explanations": None,
        "explanation_source": None,
        "gemini_status": None,
        "gemini_error": None,
        "gemini_model_used": None,
        "gemini_models_attempted": [],
        "validation_errors": [],
        "requires_administrative_review": False,
        "administrative_notes": [],
        "message": None,
        "notice": NOTICE,
    }


def explain_rejection(eligibility_result: dict) -> dict:
    eligibility_result = copy.deepcopy(validate_eligibility_result(eligibility_result))
    out = _base_result(eligibility_result)

    if eligibility_result["recommendation"] != "REJECT":
        out["status"] = "not_applicable"
        out["message"] = (
            f"The eligibility recommendation is {eligibility_result['recommendation']}; "
            "rejection explanations are only generated for REJECT."
        )
        return out

    knowledge_base = load_rejection_knowledge_base()
    evidence = eligibility_result["evidence"]
    failures = identify_confirmed_failures(evidence, knowledge_base)
    out["confirmed_failed_requirements"] = failures
    out["verified_rejection_keywords"] = [f["keyword"] for f in failures]
    out["unresolved_checks_not_used_as_reasons"] = identify_unresolved_checks(evidence)

    if not failures:
        out["status"] = "explanation_unavailable"
        out["requires_administrative_review"] = True
        out["message"] = (
            "The eligibility recommendation is REJECT, but no confirmed failed requirement was "
            "found in the verified evidence, so no rejection reason can be suggested. The "
            "original recommendation is unchanged; administrative review is required."
        )
        return out

    out.update(generate_rejection_explanations(eligibility_result, failures, knowledge_base))
    if any(f.get("requires_policy_confirmation") for f in failures):
        out["requires_administrative_review"] = True
        out["administrative_notes"].append(DEVELOPMENT_RULE_NOTE)
    if out["status"] == "fallback":
        out["message"] = "Gemini output was unavailable or failed validation; deterministic explanations were used."
    return out


def _wrap(text, indent="    "):
    return textwrap.fill(str(text), width=_WIDTH, initial_indent=indent, subsequent_indent=indent)


def print_report(r: dict) -> None:
    print("=" * _WIDTH)
    print("REJECTION EXPLANATION".center(_WIDTH))
    print("=" * _WIDTH)
    print(f"  {'Request ID':<27}: {r['request_id']}")
    print(f"  {'Eligibility recommendation':<27}: {r['eligibility_recommendation']}")
    print(f"  {'Explanation status':<27}: {r['status']}")
    if r["status"] == "not_applicable":
        print(_wrap(r["message"], "  "))
        print("=" * _WIDTH)
        return
    if r.get("explanation_source"):
        print(f"  {'Explanation source':<27}: {r['explanation_source']}")
    if r.get("gemini_models_attempted"):
        print(f"  {'Gemini model used':<27}: {r.get('gemini_model_used')}")
        print(f"  {'Gemini models attempted':<27}: {', '.join(a['model'] for a in r['gemini_models_attempted'])}")
    if r.get("gemini_error"):
        print(f"  {'Gemini error':<27}: {r['gemini_error']}")
    for err in r.get("validation_errors") or []:
        print(f"  {'Validation error':<27}: {err}")

    print("\n  Verified rejection keywords:")
    for k in r["verified_rejection_keywords"] or ["(none)"]:
        print(f"    - {k}")

    print("\n  Confirmed failed requirements:")
    if not r["confirmed_failed_requirements"]:
        print("    (none)")
    for f in r["confirmed_failed_requirements"]:
        print(f"    [{f['requirement']}] {f['label']}")
        print(_wrap(f["plain_reason"], "      "))

    if r["unresolved_checks_not_used_as_reasons"]:
        print("\n  Unresolved checks (NOT used as rejection reasons):")
        for u in r["unresolved_checks_not_used_as_reasons"]:
            print(f"    - {u['check']}: {u['issue']}")

    if r["explanations"]:
        for tone in TONES:
            print(f"\n  {tone.capitalize()} explanation:")
            print(_wrap(r["explanations"][tone]["text"]))

    if r.get("message"):
        print("\n" + _wrap(r["message"], "  "))
    for note in r.get("administrative_notes") or []:
        print("\n" + _wrap("NOTE: " + note, "  "))
    if r["requires_administrative_review"]:
        print("\n  ADMINISTRATIVE REVIEW REQUIRED")
    print("\n" + _wrap(r["notice"], "  "))
    print("=" * _WIDTH)


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python -m app.rejection_agent <request_id | path-to-saved-eligibility-output>")
        sys.exit(1)
    arg = sys.argv[1]
    try:
        if os.path.isfile(arg):
            eligibility_result = load_eligibility_result(arg)
        else:  # treat as a Request ID: run the eligibility agent first
            from app.agent import evaluate_raise_capacity_request
            eligibility_result = evaluate_raise_capacity_request(arg)
        result = explain_rejection(eligibility_result)
    except AgentError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
    print_report(result)
    print("\nFULL JSON RESULT")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
