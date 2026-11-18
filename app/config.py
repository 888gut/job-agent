"""Paths, environment and default settings."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA = Path(os.getenv("JOB_AGENT_DATA", ROOT / "data"))  # tests point this at a temp folder
RESUMES = DATA / "resumes"
TAILORED = RESUMES / "tailored"     # PDFs exported from tailored Google Docs
MASTERS = RESUMES / "masters"       # intermediate .docx/.pdf of the Google Doc masters
TOKENS = DATA / "tokens"
BROWSER_PROFILE = DATA / "browser_profile"
WEB = ROOT / "web"

for _p in (DATA, RESUMES, TAILORED, MASTERS, TOKENS, BROWSER_PROFILE):
    _p.mkdir(parents=True, exist_ok=True)

DB_URL = os.getenv("JOB_AGENT_DB", f"sqlite:///{(DATA / 'agent.db').as_posix()}")
GOOGLE_CLIENT_FILE = Path(os.getenv("GOOGLE_CLIENT_SECRET_FILE", "credentials.json"))
if not GOOGLE_CLIENT_FILE.is_absolute():
    GOOGLE_CLIENT_FILE = ROOT / GOOGLE_CLIENT_FILE

HANDSHAKE_BASE = "https://app.joinhandshake.com"

DEFAULT_SETTINGS = {
    # Search
    "queries": ["software engineer intern", "data analyst"],
    "location": "",
    "remote_ok": True,
    "max_new_per_run": 20,
    "top_n": 10,              # how many best matches the dashboard lists for Start
    # Scoring / tailoring
    "min_score": 60,          # below this a job is skipped
    # Titles never worth applying to (whole words, case-insensitive); also part-time/consulting roles
    "exclude_titles": ["intern", "internship", "co-op", "account executive", "sales representative",
                       "sales rep", "content", "accounts payable", "front desk", "receptionist",
                       "office coordinator", "studio coordinator", "senior", "sr", "manager",
                       "technical business analyst"],
    "skip_part_time": True,
    "max_commute_minutes": 75,  # on-site/hybrid jobs farther than this from Location are capped
    "tailor_below": 80,       # scores in [min_score, tailor_below) get a tailored resume
    "model": "claude-opus-5-5",
    # Applying
    "daily_cap": 15,
    "dry_run": True,          # stop right before the final Submit click
    "cover_letters": True,    # write a cover letter when an application asks for one
    "cover_letters_always": False,  # ...or for every application that has a place for one
    "employer_auto_submit": True,  # apply on employers' own sites too (you said no approval needed)
    # Schedule
    "schedule_enabled": False,
    "schedule_hours": 4,
    # Storage
    "sheet_id": "",
    "drive_folder_id": "",
}
