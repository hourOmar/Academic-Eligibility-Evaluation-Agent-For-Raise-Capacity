"""Generates and validates the three suggested rejection explanations.

Gemini's output is accepted only if ALL three explanations (formal, concise, supportive)
pass validation. If any one fails -- or Gemini is unavailable -- all three are replaced by
deterministic template explanations built from the rejection keyword knowledge base
(all-or-nothing, so the three tones never mix sources).

An explanation fails validation when it:
    - is empty
    - lists a keyword id that is not a verified rejection keyword, or omits one
    - contains a number (e.g. course ID, hours, capacity) that is not in the context
    - discusses a requirement that did not fail (topic_terms in the knowledge base)
    - claims an official rejection, or mentions AI/Gemini/automation
    - for an OR prerequisite, implies that every option is required
    - for a development-only completed-hours rule, presents it as official policy
    - contains the student's name or ID
    - (concise only) is longer than _CONCISE_MAX_WORDS words
"""

import re

from app.services import rejection_gemini

TONES = ("formal", "concise", "supportive")
_CONCISE_MAX_WORDS = 60
_NUMBER = re.compile(r"\d+")
_REQUEST_TYPE = re.compile(r"raise[\s-]+capacity", re.IGNORECASE)
_FORBIDDEN_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bhas\s+been\s+(officially\s+)?rejected\b",
        r"\b(is|was)\s+(hereby\s+)?rejected\b",
        r"\bofficially\b",
        r"\bhereby\b",
        r"\bAI\b",
        r"\bgemini\b",
        r"\bartificial intelligence\b",
        r"\bautomated\b",
    )
]
# Only checked when the prerequisite is an OR relationship: any one option is enough, so
# wording that implies every listed course is required is unsupported.
_OR_ALL_REQUIRED_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (r"\bboth\b", r"\ball of\b", r"\beach of\b", r"\ball (the |these )?(listed )?(prerequisite )?courses\b")
]
# Only checked when a completed-hours threshold is a development rule (not 'approved'):
# it must not be presented as confirmed university policy.
_OFFICIAL_POLICY_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(university|official|academic)\s+(policy|policies|regulation|regulations|rule|rules|requirement|requirements)\b",
        r"\buniversity requires\b",
        r"\b(this|the) course requires\b",
    )
]


def build_generation_context(eligibility_result: dict, failures: list[dict]) -> dict:
    evidence = eligibility_result["evidence"]
    course = evidence.get("course") or {}
    return {
        "request_type": "Raise Capacity",
        "course": {"course_id": course.get("Course_ID"), "course_name": course.get("Course_Name")},
        "section_id": evidence.get("section_id"),
        "verified_failures": [
            {k: f[k] for k in ("keyword", "label", "policy_basis", "plain_reason", "supportive_guidance", "evidence")}
            for f in failures
        ],
        "permitted_keywords": [f["keyword"] for f in failures],
    }


def _numbers(obj) -> set[int]:
    return {int(n) for n in _NUMBER.findall(str(obj))}


def _validate_one(tone: str, item, context: dict, failures: list[dict], topic_terms: dict,
                  personal_identifiers: list[str]) -> list[str]:
    if not isinstance(item, dict):
        return [f"{tone}: missing explanation"]
    text = (item.get("text") or "").strip()
    if not text:
        return [f"{tone}: empty text"]

    errors = []
    verified = {f["keyword"] for f in failures}
    declared = set(item.get("addresses_keywords") or [])
    if declared - verified:
        errors.append(f"{tone}: unsupported keyword(s) {sorted(declared - verified)}")
    if verified - declared:
        errors.append(f"{tone}: does not address verified keyword(s) {sorted(verified - declared)}")

    unknown_numbers = _numbers(text) - _numbers(context)
    if unknown_numbers:
        errors.append(f"{tone}: contains number(s) not in the verified evidence {sorted(unknown_numbers)}")

    failed_requirements = {f["requirement"] for f in failures}
    topic_text = _REQUEST_TYPE.sub(" ", text).lower()
    for requirement, terms in topic_terms.items():
        if requirement not in failed_requirements and any(t.lower() in topic_text for t in terms):
            errors.append(f"{tone}: discusses '{requirement}', which is not a confirmed failure")

    for pattern in _FORBIDDEN_PATTERNS:
        if pattern.search(text):
            errors.append(f"{tone}: contains disallowed wording ({pattern.pattern})")

    if "prerequisite_options_not_satisfied" in verified:
        for pattern in _OR_ALL_REQUIRED_PATTERNS:
            if pattern.search(text):
                errors.append(f"{tone}: implies every OR prerequisite option is required ({pattern.pattern})")

    if any(f.get("requires_policy_confirmation") for f in failures):
        for pattern in _OFFICIAL_POLICY_PATTERNS:
            if pattern.search(text):
                errors.append(f"{tone}: presents a development rule as official policy ({pattern.pattern})")

    for identifier in personal_identifiers:
        if identifier and identifier.lower() in text.lower():
            errors.append(f"{tone}: contains the student's personal information")

    if tone == "concise" and len(text.split()) > _CONCISE_MAX_WORDS:
        errors.append(f"concise: longer than {_CONCISE_MAX_WORDS} words")
    return errors


def validate_explanations(explanations, context: dict, failures: list[dict], topic_terms: dict,
                          personal_identifiers: list[str]) -> list[str]:
    if not isinstance(explanations, dict):
        return ["no explanations returned"]
    errors = []
    for tone in TONES:
        errors.extend(_validate_one(tone, explanations.get(tone), context, failures, topic_terms,
                                    personal_identifiers))
    return errors


def build_fallback_explanations(context: dict, failures: list[dict], templates: dict) -> dict:
    keywords = [f["keyword"] for f in failures]
    guidance = " ".join(f["supportive_guidance"] for f in failures if f["supportive_guidance"])
    values = {
        "course_id": context["course"]["course_id"],
        "course_name": context["course"]["course_name"],
        "section_id": context["section_id"],
        "plain_reasons": " ".join(f["plain_reason"] for f in failures),
        "concise_reasons": "; ".join(f["concise_reason"] for f in failures),
        "supportive_guidance": guidance,
    }
    return {
        tone: {"text": " ".join(templates[tone].format(**values).split()), "addresses_keywords": list(keywords)}
        for tone in TONES
    }


def generate_rejection_explanations(eligibility_result: dict, failures: list[dict], knowledge_base: dict) -> dict:
    context = build_generation_context(eligibility_result, failures)
    student = eligibility_result["evidence"].get("student") or {}
    personal_identifiers = [str(v) for v in (student.get("Student_Name"), student.get("Student_ID")) if v]

    raw = rejection_gemini.get_rejection_explanations(context)
    model_info = {
        "gemini_model_used": raw.get("model_used"),
        "gemini_models_attempted": raw.get("models_attempted") or [],
    }
    validation_errors: list[str] = []
    if raw["status"] == "ok":
        validation_errors = validate_explanations(
            raw["explanations"], context, failures, knowledge_base["topic_terms"], personal_identifiers
        )
        if not validation_errors:
            return {
                "status": "generated",
                "explanation_source": "gemini",
                "explanations": {
                    tone: {"text": raw["explanations"][tone]["text"].strip(),
                           "addresses_keywords": list(raw["explanations"][tone]["addresses_keywords"])}
                    for tone in TONES
                },
                "gemini_status": "ok",
                "gemini_error": None,
                **model_info,
                "validation_errors": [],
            }

    return {
        "status": "fallback",
        "explanation_source": "deterministic_fallback",
        "explanations": build_fallback_explanations(context, failures, knowledge_base["fallback_templates"]),
        "gemini_status": raw["status"] if raw["status"] != "ok" else "invalid_output",
        "gemini_error": raw["error"],
        **model_info,
        "validation_errors": validation_errors,
    }
