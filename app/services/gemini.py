"""Gemini is the decision-making layer of the Academic Eligibility Evaluation Agent.

Python's job (see app/agent.py and app/tools/*) is ONLY to collect and compute verified,
deterministic evidence:
    - retrieve request/student/transcript data from the database (read-only)
    - retrieve and parse the course prerequisite from the official UoS website
    - classify grades against the approved grade policy
    - read the local completed-hours knowledge base
    - calculate capacity numbers

Python does NOT combine these into a final APPROVE/REJECT/MANUAL_REVIEW recommendation.
That combination is Gemini's job: it is given the full verified evidence package plus the
explicit decision rules, and must produce the recommendation and an explanation for it.
Gemini must use only the supplied evidence -- it cannot invent facts, cannot recompute
capacity (Python's numbers are final), and cannot query the database or browse the web.

Structured output (response_schema) is used so the response is always JSON-shaped and
predictable.
"""

import json
from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel

from app import config
from app.exceptions import GeminiUnavailableError

_client = None


class CheckExplanations(BaseModel):
    prerequisite: str
    completed_hours: str
    capacity: str


class EligibilityDecision(BaseModel):
    recommendation: Literal["APPROVE", "REJECT", "MANUAL_REVIEW"]
    reasoning: str
    checks: CheckExplanations


def _get_client():
    global _client
    if _client is None:
        if not config.GEMINI_API_KEY:
            raise GeminiUnavailableError("GEMINI_API_KEY is not configured. Set it in .env.")
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def _build_prompt(evidence_package: dict) -> str:
    evidence_json = json.dumps(evidence_package, indent=2, default=str)
    return f"""You are the Academic Eligibility Evaluation Agent for University of Sharjah
Raise Capacity requests. You decide the final recommendation -- APPROVE, REJECT, or
MANUAL_REVIEW -- by applying the decision rules below to the verified evidence provided.

All evidence below was ALREADY collected and computed deterministically in Python from
verified sources: the official University of Sharjah website (prerequisite), the student's
transcript and the approved grade policy (prerequisite satisfaction), the local
completed-hours knowledge base, and the Section table (capacity). Do NOT recompute,
re-derive, or second-guess any of these values -- especially the capacity numbers, which
are already final. Do NOT invent any prerequisite, grade, threshold, or capacity number
that is not present in the evidence below. Do NOT query any database or browse the web --
everything you need is in the evidence below.

DECISION RULES (apply exactly; use no other rule):

APPROVE requires ALL of:
- checks.prerequisite.verified is true AND checks.prerequisite.satisfied is true
- the completed-hours requirement is satisfied WHENEVER it applies -- if
  checks.completed_hours.applicable is false, this condition is automatically met (no rule
  is configured for this course, which is not a failure)
- checks.capacity.satisfied is true (additional section capacity is available)

REJECT applies when ANY of:
- checks.prerequisite.verified is true AND checks.prerequisite.satisfied is false (a
  CONFIRMED, verified failure -- not merely unresolved)
- checks.completed_hours.applicable is true AND checks.completed_hours.satisfied is false
- checks.capacity.satisfied is false (no additional capacity remains)

MANUAL_REVIEW applies whenever neither APPROVE nor REJECT can be conclusively determined,
for example:
- checks.prerequisite.verified is false (could not be verified from the UoS source)
- checks.prerequisite.satisfied is null (unresolved -- e.g. a grade value outside the
  approved policy, or a text-only condition with no course ID)
- checks.completed_hours.applicable is true AND checks.completed_hours.satisfied is null
  (the student's completed hours or the configured threshold is missing/invalid)
- checks.capacity.satisfied is null (the Section capacity record is invalid)
- any other required input is missing or ambiguous

CRITICAL RULE: missing or unverifiable evidence is NEVER the same as a failed requirement.
It must lead to MANUAL_REVIEW, never REJECT. REJECT requires a CONFIRMED, verified failure.

Verified evidence:
{evidence_json}

Produce the structured response:
- recommendation: APPROVE, REJECT, or MANUAL_REVIEW, decided strictly by the rules above.
- reasoning: 3-5 sentences explaining the overall decision and which rule(s) it followed.
- checks.prerequisite: explain the prerequisite outcome, citing the specific evidence
  (course IDs, verification/satisfaction status, grades where relevant) that produced it.
  If additional_text_conditions is non-empty, mention it separately as a condition that
  exists but was not itself evaluated.
- checks.completed_hours: if applicable is false, clearly state that no completed-hours
  rule is configured for this course (not a failure). If applicable, cite the student's
  completed hours against the required hours.
- checks.capacity: cite the actual current/total/additional/maximum/remaining capacity
  numbers and state whether additional space remains."""


def get_gemini_decision(evidence_package: dict) -> dict:
    try:
        client = _get_client()
        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=_build_prompt(evidence_package),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=EligibilityDecision,
            ),
        )
        parsed: EligibilityDecision | None = getattr(response, "parsed", None)
        if parsed is None:
            raise GeminiUnavailableError("Gemini's structured response could not be parsed.")

        return {
            "status": "ok",
            "recommendation": parsed.recommendation,
            "reasoning": parsed.reasoning,
            "checks": {
                "prerequisite": parsed.checks.prerequisite,
                "completed_hours": parsed.checks.completed_hours,
                "capacity": parsed.checks.capacity,
            },
            "error": None,
        }
    except GeminiUnavailableError as exc:
        return _unavailable_result(evidence_package, str(exc))
    except Exception as exc:  # noqa: BLE001 - any Gemini/client failure must degrade to MANUAL_REVIEW, never crash
        return _unavailable_result(evidence_package, str(exc))


def _unavailable_result(evidence_package: dict, error: str) -> dict:
    """Gemini IS the decision-maker in this architecture. If it cannot be reached, there is
    no deterministic fallback recommendation -- Python does not compute one. The only safe
    answer is MANUAL_REVIEW: evidence was collected, but no automated decision was produced."""
    checks = evidence_package.get("checks", {})
    return {
        "status": "unavailable",
        "recommendation": "MANUAL_REVIEW",
        "reasoning": (
            f"Gemini (the decision-making layer) was unavailable, so no automated "
            f"recommendation could be produced: {error}. Verified evidence was collected "
            "and is available below for manual review."
        ),
        "checks": {
            "prerequisite": f"Not reviewed by Gemini. Evidence collected: {checks.get('prerequisite')}",
            "completed_hours": f"Not reviewed by Gemini. Evidence collected: {checks.get('completed_hours')}",
            "capacity": f"Not reviewed by Gemini. Evidence collected: {checks.get('capacity')}",
        },
        "error": error,
    }
