import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

# Confirmed by direct inspection (Phase 1) to be a server-rendered page containing the
# "Degree Requirements" tables (Course #, Course Title, Cr. Hr., Prerequisite) as plain HTML.
UOS_CS_DEGREE_URL = "https://www.sharjah.ac.ae/en/Academics/Degree/Undergraduate/Computer-Science"
