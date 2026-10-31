"""Combined Raise Capacity pipeline: Academic Eligibility Evaluation Agent followed, only
for REJECT, by the Rejection Explanation Agent.

    Request ID
    -> Academic Eligibility Evaluation Agent (app/agent.py, unchanged)
       evidence retrieval -> deterministic checks -> Gemini recommendation
    -> APPROVE        : return the eligibility result (+ consistency warning if a
                        deterministic check has a confirmed failure)
    -> REJECT         : Rejection Explanation Agent (app/rejection_agent.py)
    -> MANUAL_REVIEW  : return the eligibility result + unresolved evidence

The two agents stay separate: this module only routes between them. The eligibility
recommendation is never changed; the final decision rests with authorized
administrative staff.

Run with:  python -m app.pipeline <request_id> [--no-json]
"""

import copy
import json
import sys

from app.agent import _print_report as print_eligibility_report
from app.agent import evaluate_raise_capacity_request
from app.exceptions import AgentError
from app.rejection_agent import explain_rejection
from app.rejection_agent import print_report as print_rejection_report
from app.tools.rejection_keyword_tools import (
    identify_confirmed_failures,
    identify_unresolved_checks,
    load_rejection_knowledge_base,
)

FINAL_DECISION_BY = "Authorized administrative staff"
_WIDTH = 70


def _unresolved_evidence(eligibility_result: dict) -> list[dict]:
    unresolved = identify_unresolved_checks(eligibility_result["evidence"])
    if eligibility_result.get("gemini_status") != "ok":
        unresolved.append({
            "check": "gemini_decision", "issue": "unavailable",
            "details": eligibility_result.get("gemini_error"),
        })
    return unresolved


def _consistency_warnings(eligibility_result: dict, rejection: dict | None) -> list[dict]:
    warnings = []
    if eligibility_result["recommendation"] == "APPROVE":
        failures = identify_confirmed_failures(eligibility_result["evidence"], load_rejection_knowledge_base())
        if failures:
            warnings.append({
                "type": "approve_with_confirmed_failure",
                "message": (
                    "The recommendation is APPROVE, but the deterministic evidence shows a confirmed "
                    "failed requirement. The recommendation has not been changed; administrative "
                    "review is required."
                ),
                "confirmed_failed_keywords": [f["keyword"] for f in failures],
            })
    if rejection is not None and rejection["status"] == "explanation_unavailable":
        warnings.append({
            "type": "reject_without_confirmed_failure",
            "message": rejection["message"],
            "confirmed_failed_keywords": [],
        })
    return warnings


def run_pipeline(request_id: str) -> dict:
    eligibility = evaluate_raise_capacity_request(request_id)
    recommendation = eligibility["recommendation"]

    rejection = None
    if recommendation == "REJECT":
        # The agent also deep-copies internally; the copy here keeps the pipeline's own
        # eligibility result safe regardless of what the other agent does.
        rejection = explain_rejection(copy.deepcopy(eligibility))

    warnings = _consistency_warnings(eligibility, rejection)
    return {
        "request_id": request_id,
        "recommendation": recommendation,
        "academic_eligibility_evaluation": eligibility,
        "rejection_explanation": rejection,
        "rejection_explanation_status": rejection["status"] if rejection else "not_run",
        "unresolved_evidence": _unresolved_evidence(eligibility) if recommendation == "MANUAL_REVIEW" else [],
        "consistency_warnings": warnings,
        "administrative_review_required": (
            recommendation == "MANUAL_REVIEW"
            or bool(warnings)
            or bool(rejection and rejection["requires_administrative_review"])
        ),
        "final_decision_by": FINAL_DECISION_BY,
    }


def print_pipeline_report(result: dict) -> None:
    print_eligibility_report(result["academic_eligibility_evaluation"], show_json=False)
    print()
    recommendation = result["recommendation"]
    if result["rejection_explanation"] is not None:
        print_rejection_report(result["rejection_explanation"])
    else:
        print("-" * _WIDTH)
        print(f"Recommendation is {recommendation}: the Rejection Explanation Agent was not run.")
        if result["unresolved_evidence"]:
            print("\nUNRESOLVED EVIDENCE")
            for u in result["unresolved_evidence"]:
                print(f"  - {u['check']}: {u['issue']} -- {u.get('details')}")
    for w in result["consistency_warnings"]:
        print(f"\nCONSISTENCY WARNING ({w['type']}): {w['message']}")
    print("\n" + "-" * _WIDTH)
    print(f"  {'Administrative review flag':<27}: {result['administrative_review_required']}")
    print(f"  {'Final decision by':<27}: {result['final_decision_by']}")
    print("-" * _WIDTH)


def main() -> None:
    args = sys.argv[1:]
    show_json = "--no-json" not in args
    args = [a for a in args if a != "--no-json"]
    if len(args) != 1:
        print("Usage: python -m app.pipeline <request_id> [--no-json]")
        sys.exit(1)
    try:
        result = run_pipeline(args[0])
    except AgentError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
    print_pipeline_report(result)
    if show_json:
        print("\nFULL JSON RESULT")
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
