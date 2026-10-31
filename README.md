# Academic Eligibility Evaluation Agent (Prototype)

Standalone prototype for the Raise Capacity Academic Eligibility Evaluation Agent, built
outside the main Smart UoS E-Service repository to build, test, and validate the logic in
isolation. It will later be integrated into the Smart UoS E-Service backend.

## Purpose

Given a `Raise_Capacity` request ID, the agent evaluates three required conditions and
returns one recommendation: `APPROVE`, `REJECT`, or `MANUAL_REVIEW`.

1. The student satisfies the course prerequisite (verified from the official UoS website).
2. The student satisfies the completed-hours rule for that course, if one is configured.
3. The requested section still has additional Raise Capacity space available.

The agent never invents grades, prerequisites, completed-hour thresholds, or capacity
values. Missing or unverifiable evidence always routes to `MANUAL_REVIEW` -- it is never
silently treated as a failure.

## Architecture

- **Python**, run as a local CLI (no FastAPI, no LangGraph, no frontend integration yet).
- **Database**: Supabase PostgreSQL, accessed **read-only** via SQLAlchemy 2.x + `psycopg`
  3. Every query uses quoted mixed-case identifiers and bound parameters. No row is ever
  inserted, updated, or deleted.
- **Prerequisites**: retrieved live from the official University of Sharjah Computer
  Science degree page (`app/config.py:UOS_CS_DEGREE_URL`). This page was confirmed to be
  server-rendered HTML (a plain `<table>` inside `#degree-requirements`, no JavaScript
  rendering) via direct inspection, so retrieval uses `requests` + `BeautifulSoup4` --
  no browser automation.
- **Completed-hours rules**: a local, manually-curated JSON file
  (`app/knowledge_base/course_completed_hours.json`), entirely separate from UoS
  prerequisites. A course absent from this file has no configured rule and is treated as
  **not applicable**, never as a failure.
- **Gemini** (`google-genai`): **Gemini is the decision-making layer.** Python only
  collects and computes verified, deterministic evidence (prerequisite retrieval +
  evaluation, completed-hours lookup + evaluation, capacity arithmetic) -- it does **not**
  combine that evidence into a final recommendation itself. The full evidence package plus
  explicit decision rules are sent to Gemini, which returns a structured
  (`response_schema` = `EligibilityDecision` in `app/services/gemini.py`) recommendation
  (`APPROVE` / `REJECT` / `MANUAL_REVIEW`) and an explanation, so the reply is always
  JSON-shaped and predictable, never free-form prose to re-parse. Gemini must use only the
  supplied evidence: it cannot invent facts, cannot recompute or second-guess the capacity
  numbers Python already calculated, and cannot query the database or browse the web. The
  workflow is fixed (see `app/agent.py`) -- Gemini never chooses which tool to call, and is
  not given tool access. If Gemini is unreachable, there is no deterministic fallback
  recommendation to substitute -- the result is `MANUAL_REVIEW` with the collected evidence
  attached, never a guess.

## How a decision is made

`app/services/gemini.py:get_gemini_decision` sends Python's evidence package to Gemini
along with the explicit rules below; Gemini applies them and returns the recommendation:

- **REJECT** if any check has a *confirmed* failure: prerequisite verified-and-unsatisfied,
  a configured completed-hours rule that isn't met, or no remaining capacity.
- **MANUAL_REVIEW** if there is no confirmed failure but something critical could not be
  verified: the UoS prerequisite page was unreachable/unparseable, the course wasn't
  found on the page, its prerequisite is a nested/parenthesized expression, a required
  course has a grade outside the approved grade policy (see below), the student's
  completed hours are missing/invalid while a completed-hours rule applies, or the
  Section capacity record is invalid (missing or negative values).
- **APPROVE** only when every applicable check is verified and satisfied.

Python still computes the per-check `verified`/`satisfied` booleans deterministically
(prerequisite, completed-hours, capacity) -- Gemini is only responsible for combining
those three already-verified results into the final three-way recommendation per the rules
above, not for evaluating the underlying evidence itself.

### Prerequisite relationship types are preserved, not flattened

`get_course_prerequisites` parses the UoS "Prerequisite" column into a structured form
that keeps the real relationship:

| UoS text | `relationship_type` | `prerequisite_groups` |
|---|---|---|
| `None` | `NONE` | `[]` |
| `1501211` | `SINGLE` | `[[1501211]]` |
| `1501215, 1501279` | `AND` | `[[1501215, 1501279]]` |
| `1501211 or 1501341` | `OR` | `[[1501211], [1501341]]` |
| `Pre/Co: 1430110` | `SINGLE` + `is_pre_or_co=true` | `[[1430110]]` |
| `1501215 and Junior Standing` | `SINGLE` | `[[1501215]]`, with `additional_text_conditions: ["Junior Standing"]` |
| `1501341, (1501246 or 1501247)` | not parsed -- `verified: false` | nested/parenthesized expressions are not supported and route to `MANUAL_REVIEW` |

Text conditions like "Junior Standing" are **never** converted into a credit-hour number
-- they are surfaced as-is in `additional_text_conditions` for transparency and are not
auto-evaluated (the local completed-hours knowledge base's `required_standing` field is
the intended place for that, as a separate, explicitly-configured rule).

### Grade policy (prerequisite evaluation)

`app/knowledge_base/grade_policy.json` holds the approved grade policy, loaded by
`app/tools/grade_policy.py` and used by the deterministic prerequisite evaluation:

- **Passing grades:** `A`, `B+`, `B`, `C+`, `C`, `D+`, `D` -- a required course counts as
  passed if any recorded attempt has one of these grades.
- **Failing grades:** `F`, `WF`, `FA`.
- **NULL grade** (course in progress / not completed) does **not** count as a passed
  prerequisite.
- A required course that **never appears** in the transcript is not passed.
- Any grade value **outside the policy** (e.g. `W`, `I`) is never guessed: the
  prerequisite is left unresolved (`prerequisite_satisfied: null`,
  `needs_manual_review: true`) and routes to `MANUAL_REVIEW`.

### Completed-hours: not-applicable vs. not-satisfied

- Course **is** in `course_completed_hours.json` -> evaluated normally
  (`applicable: true, requirement_found: true, satisfied: true|false`, where satisfied
  means completed hours >= `minimum_completed_hours`). Course IDs are compared as strings,
  since `Course_ID` is a text column in the database.
- Course **is** in the file but the student's `Completed_Hours` is NULL/invalid ->
  `applicable: true, satisfied: null`, which routes to `MANUAL_REVIEW`.
- Course **is not** in the file -> `applicable: false, requirement_found: false,
  satisfied: null`. This is not a rejection and not an automatic manual review; it means
  no special rule is configured for that course.

Currently configured: `1501215` Data Structures (31 hours) and `1501394` Junior Project in
CS (65 hours). These are marked `development_rule`.

### Capacity

`maximum_allowed_capacity = Total_Capacity + Additional_Capacity`, and additional capacity
is available when `Current_Capacity < maximum_allowed_capacity`. A capacity record with a
missing, non-integer, or negative value is treated as invalid (`satisfied: null`) and
routes to `MANUAL_REVIEW`.

## Rejection Explanation Agent and combined pipeline

A second, separate agent (`app/rejection_agent.py`) runs **only when the eligibility
recommendation is `REJECT`**. It never changes the recommendation: it works on a deep copy
of the eligibility result and returns its own, separate block.

1. **Confirmed failures only.** Failures are read deterministically from
   `evidence.checks` -- prerequisite `verified=true` and `satisfied=false`, completed hours
   `applicable=true` and `satisfied=false`, capacity `satisfied=false`. Unresolved evidence
   (`satisfied=null`, or an unverified prerequisite) is listed separately and is never used
   as a reason.
2. **Keywords.** Each confirmed failure maps to a keyword in
   `app/knowledge_base/rejection_keywords.json` (`prerequisite_not_completed`,
   `prerequisite_not_passed`, `prerequisite_options_not_satisfied`,
   `completed_hours_insufficient`, `no_remaining_capacity`). Prerequisite keywords follow
   the UoS relationship type: for SINGLE/AND every listed course is required; for OR a
   single `prerequisite_options_not_satisfied` reason states that any one option is
   enough. A completed-hours threshold whose status in `course_completed_hours.json` is
   not `approved` (currently both rules are `development_rule`) is worded as "the minimum
   applied in this evaluation", never as university policy, and the result is flagged
   for administrative review with a note to confirm the rule.
3. **Gemini wording (separate call).** `app/services/rejection_gemini.py` reuses the same
   Gemini client and sends only the confirmed failures, their verified evidence, and the
   permitted keywords (no student name or ID). It returns `formal`, `concise`, and
   `supportive` suggestions.
4. **Validation (all-or-nothing).** `app/tools/explanation_generator.py` rejects Gemini's
   output if any of the three explanations is empty, cites an unknown keyword or omits a
   verified one, contains a number not in the evidence, discusses a requirement that did
   not fail, claims an official rejection or mentions AI/Gemini, or contains the student's
   name/ID. If any one fails -- or Gemini is unavailable -- all three are replaced by
   deterministic template explanations from the knowledge base.
5. If the recommendation is `REJECT` but no confirmed failure exists, the status is
   `explanation_unavailable` and the result is flagged for administrative review.

`app/pipeline.py` routes between the two agents:

| Recommendation | Pipeline behaviour |
|---|---|
| `APPROVE` | Eligibility result only. If a deterministic check nevertheless shows a confirmed failure, a `consistency_warnings` entry is added and `administrative_review_required` is set -- the recommendation is not changed. |
| `REJECT` | Eligibility result + `rejection_explanation` (status `generated`, `fallback`, or `explanation_unavailable`). |
| `MANUAL_REVIEW` | Eligibility result + `unresolved_evidence`. No rejection explanation is generated. |

The explanations are suggestions only; authorized administrative staff make the final
decision.

## Project layout

```
app/
  config.py                  Env config + UoS URL
  agent.py                   Academic Eligibility Evaluation Agent: fixed workflow + CLI entry point
  rejection_agent.py         Rejection Explanation Agent (REJECT only) + CLI entry point
  pipeline.py                Combined pipeline: eligibility -> (REJECT) rejection explanation
  exceptions.py              Typed errors for every failure mode
  services/
    database.py               Read-only SQLAlchemy engine + generic Course lookup
    gemini.py                 Final decision layer: APPROVE/REJECT/MANUAL_REVIEW + explanation from the verified evidence
    rejection_gemini.py       Separate Gemini call: formal/concise/supportive rejection wording
  tools/
    rejection_input_tools.py  validate/load an eligibility result
    rejection_keyword_tools.py identify_confirmed_failures(), identify_unresolved_checks()
    explanation_generator.py  Gemini output validation + deterministic fallback explanations
    request_tools.py          get_raise_capacity_request()
    student_tools.py          get_student_academic_info(), get_student_transcript()
    prerequisite_tools.py     get_course_prerequisites() [UoS scraper], evaluate_prerequisite_status()
    grade_policy.py           classify_grade() using grade_policy.json
    hours_tools.py            get_completed_hours_requirement(), evaluate_completed_hours()
    capacity_tools.py         get_section_capacity(), evaluate_capacity()
  knowledge_base/
    course_completed_hours.json
    grade_policy.json
    rejection_keywords.json   Closed rejection keyword vocabulary + fallback templates
tests/
  test_eligibility.py         Automated tests (pytest; no database, Gemini, or network needed)
  test_rejection_agent.py     Rejection Explanation Agent tests (fake Gemini)
  test_pipeline.py            Combined pipeline tests (all data sources and both Gemini calls faked)
  test_agent.py               CLI-style harness for a real request (needs .env)
pytest.ini
```

## Configuring `.env`

Copy `.env.example` to `.env` and fill in:

```
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3.6-flash
GEMINI_FALLBACK_MODELS=
DATABASE_URL=
```

- `DATABASE_URL` is the Supabase PostgreSQL connection string. A plain `postgresql://` or
  `postgres://` URL is automatically rewritten to the **psycopg 3** dialect
  (`postgresql+psycopg://`), so it can be pasted as-is.
- `GEMINI_MODEL` defaults to `gemini-3.6-flash` if unset.
- `GEMINI_FALLBACK_MODELS` (optional, comma-separated) is the same mechanism as the Raise
  Capacity Prioritization Agent: both Gemini calls (eligibility decision and rejection
  wording) try `[GEMINI_MODEL, *GEMINI_FALLBACK_MODELS]` in order with the identical
  request, moving to the next model **only** on a temporary 503 or 429 error. Any other
  error stops immediately. An unparseable response from a model that did answer is not a
  fallback trigger. If every model fails, eligibility returns `MANUAL_REVIEW` and the
  rejection agent uses its deterministic explanations. Both results record
  `gemini_model_used` and `gemini_models_attempted`. Empty means no fallback.
- Never commit `.env` (it's already in `.gitignore`). Never paste real credentials into
  chat, issues, or commit messages.

## Installing dependencies

```
pip install -r requirements.txt
```

## Running

```
python -m app.agent <request_id>
```

or equivalently:

```
python tests/test_agent.py <request_id>
```

Both print each stage of the pipeline (request, course, prerequisite check, completed-hours
check, capacity check) followed by the Gemini-generated reasoning, the final
recommendation, and the full JSON result.

Combined pipeline (both agents):

```
python -m app.pipeline <request_id>            # add --no-json to hide the JSON result
```

Rejection Explanation Agent alone, on a Request ID or on saved eligibility output:

```
python -m app.rejection_agent <request_id | path-to-saved-output>
```

## Running the tests

```
python -m pytest
```

The automated tests use fakes for the database, the UoS page, and both Gemini calls, so they run
from a fresh clone without a `.env`, credentials, network access, or Gemini quota.

## Read-only guarantee

This project only ever executes `SELECT` statements against Supabase, additionally
requests a read-only Postgres transaction on connect, and contains no code path that
constructs or executes an INSERT/UPDATE/DELETE. No arbitrary/user-driven SQL is ever
generated -- all queries are fixed, parameterized statements.

## Status

Prototype only. Not integrated with the Smart UoS E-Service frontend/backend. Once
validated here, the deterministic workflow and tools are intended to be ported into the
main Smart UoS E-Service backend.
