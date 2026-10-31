"""Input handling for the Rejection Explanation Agent.

The Rejection Explanation Agent never touches the database, the UoS website, or the
eligibility rules: the finished result of the Academic Eligibility Evaluation Agent
(app/agent.py:evaluate_raise_capacity_request) is its only input."""

import json

from app.exceptions import InvalidEligibilityResultError

_VALID_RECOMMENDATIONS = ("APPROVE", "REJECT", "MANUAL_REVIEW")
_REQUIRED_CHECKS = ("prerequisite", "completed_hours", "capacity")
_JSON_MARKER = "FULL JSON RESULT"


def validate_eligibility_result(result) -> dict:
    if not isinstance(result, dict):
        raise InvalidEligibilityResultError("The eligibility result must be a JSON object.")
    if result.get("recommendation") not in _VALID_RECOMMENDATIONS:
        raise InvalidEligibilityResultError(
            f"Missing or unknown 'recommendation' ({result.get('recommendation')!r}); "
            "this is not an Academic Eligibility Evaluation Agent result."
        )
    evidence = result.get("evidence")
    if not isinstance(evidence, dict) or not isinstance(evidence.get("checks"), dict):
        raise InvalidEligibilityResultError("Missing 'evidence.checks' in the eligibility result.")
    for name in _REQUIRED_CHECKS:
        if not isinstance(evidence["checks"].get(name), dict):
            raise InvalidEligibilityResultError(f"Missing 'evidence.checks.{name}' in the eligibility result.")
    return result


def parse_eligibility_text(text: str) -> dict:
    """Accepts either raw JSON or the whole terminal output of `python -m app.agent <id>`
    (the report followed by 'FULL JSON RESULT' and the JSON)."""
    if not isinstance(text, str) or not text.strip():
        raise InvalidEligibilityResultError("The eligibility result is empty.")
    idx = text.find(_JSON_MARKER)
    start = text.find("{", idx + len(_JSON_MARKER) if idx != -1 else 0)
    if start == -1:
        raise InvalidEligibilityResultError("No JSON object found in the eligibility output.")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise InvalidEligibilityResultError(f"The eligibility JSON could not be parsed: {exc}") from exc
    return obj


def load_eligibility_result(path: str) -> dict:
    """PowerShell's `>` writes UTF-16, so that is detected by its BOM."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        raise InvalidEligibilityResultError(f"Cannot read '{path}': {exc}") from exc
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    return parse_eligibility_text(text)
