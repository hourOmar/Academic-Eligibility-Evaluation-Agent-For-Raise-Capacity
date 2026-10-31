import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
# Optional ordered list of additional models to try, in order, if GEMINI_MODEL fails with a
# temporary/retryable error (503 service-unavailable, 429 rate-limit). Comma-separated, e.g.
# "gemini-3.6-flash,gemini-3.7-flash". Empty by default: no fallback, behavior unchanged.
# Same mechanism as the Raise Capacity Prioritization Agent.
GEMINI_FALLBACK_MODELS = [
    m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "").split(",") if m.strip()
]

# Confirmed by direct inspection (Phase 1) to be a server-rendered page containing the
# "Degree Requirements" tables (Course #, Course Title, Cr. Hr., Prerequisite) as plain HTML.
UOS_CS_DEGREE_URL = "https://www.sharjah.ac.ae/en/Academics/Degree/Undergraduate/Computer-Science"
