"""Gemini wording step of the Rejection Explanation Agent.

This is a SEPARATE Gemini call from the eligibility decision in app/services/gemini.py.
It reuses the same client, but Gemini here decides nothing: it receives only the
confirmed failures Python already identified (plus their verified evidence and the
permitted keyword ids) and writes three suggested explanations. Its output is then
validated by app/tools/explanation_generator.py before anything is returned.

No student name or ID is sent to Gemini.
"""

import json

from google.genai import types
from pydantic import BaseModel

from app.exceptions import GeminiRequestFailedError
from app.services.gemini import _get_client, generate_with_model_fallback


class SuggestedExplanation(BaseModel):
    text: str
    addresses_keywords: list[str]


class RejectionExplanations(BaseModel):
    formal: SuggestedExplanation
    concise: SuggestedExplanation
    supportive: SuggestedExplanation


_RULES = """You write SUGGESTED rejection explanations for a University of Sharjah Raise
Capacity request. The Academic Eligibility Evaluation Agent has already recommended REJECT,
and authorized administrative staff make the final decision. You decide nothing.

Rules:
1. Use ONLY the information in the provided context. Never invent facts, numbers, course
   IDs, grades, policies, requirements, deadlines, or procedures.
2. Explain ONLY the requirements listed in verified_failures. Do not mention requirements
   that are not listed there (do not say that other requirements were met or were not
   checked).
3. Every explanation must cover EVERY keyword in permitted_keywords and list exactly those
   ids in addresses_keywords. Never create a new id.
4. formal: professional wording suitable for official university communication.
   concise: one or two short, direct sentences.
   supportive: polite; clearly states the issue and, only where a verified failure has a
   non-empty supportive_guidance, explains what the student may need to address using that
   guidance. Do not suggest any other action.
5. Address the student in the second person. Do not include the student's name or ID.
6. Never state that the request "has been rejected" or that the university has officially
   rejected it; describe which evaluated conditions are not met.
7. Do not mention AI, Gemini, or an automated system.
8. Prerequisites: if relationship_type is OR, completing ANY ONE of the listed options is
   enough. Never say that both, each, or all of the listed courses are required. For
   SINGLE or AND, every listed course is required.
9. Completed credit hours: if evidence.rule_status is not "approved", the threshold is a
   development rule, not confirmed university policy. Describe it as the minimum applied
   in this evaluation; never call it a university policy, regulation, or requirement, and
   never say "this course requires".
"""


def _build_contents(context: dict) -> str:
    return "Context:\n" + json.dumps(context, indent=2, default=str)


def get_rejection_explanations(context: dict) -> dict:
    """Returns {"status": "ok"|"unavailable", "explanations": {...}|None, "error": str|None,
    "model_used": str|None, "models_attempted": [...]}. The same model-fallback loop as the
    eligibility call is used (retry the next configured model only on 429/503). Any
    failure degrades to "unavailable" so the caller can use the deterministic fallback."""
    out = {"status": "unavailable", "explanations": None, "error": None,
           "model_used": None, "models_attempted": []}
    try:
        client = _get_client()
        response, model_used, out["models_attempted"] = generate_with_model_fallback(
            client,
            _build_contents(context),
            types.GenerateContentConfig(
                system_instruction=_RULES,
                response_mime_type="application/json",
                response_schema=RejectionExplanations,
                temperature=0.3,
            ),
        )
        parsed: RejectionExplanations | None = getattr(response, "parsed", None)
        if parsed is None:
            out["error"] = "Gemini's structured response could not be parsed."
            return out
        if not isinstance(parsed, RejectionExplanations):
            parsed = RejectionExplanations.model_validate(parsed)
        out.update(status="ok", explanations=parsed.model_dump(), model_used=model_used)
        return out
    except GeminiRequestFailedError as exc:
        out.update(error=f"{type(exc).__name__}: {exc}", models_attempted=exc.models_attempted)
        return out
    except Exception as exc:  # noqa: BLE001 - any Gemini/client failure must degrade to the fallback, never crash
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
