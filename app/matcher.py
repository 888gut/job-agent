"""Claude: turn a raw posting page into a clean job record, and score it against both resumes."""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .llm import ask_json


class JobExtract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    company: str
    location: str
    job_type: str
    deadline: str
    description: str
    apply_type: Literal["handshake", "external", "unknown"]


class ResumeFit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot: str
    score: int
    reasons: list[str]
    gaps: list[str]


class MatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fits: list[ResumeFit]
    best_slot: str
    branch: str
    pitch: str


EXTRACT_SYSTEM = """You read the visible text of a Handshake job posting page and return a clean record.
- description: the responsibilities, qualifications and preferred skills, faithfully condensed (keep every concrete requirement; drop page chrome, navigation and unrelated listings).
- apply_type: "handshake" if the page offers a regular Apply button on Handshake, "external" if it says to apply on the employer's site / "Apply externally", otherwise "unknown".
- Use an empty string for any field the page does not show."""

SCORE_SYSTEM = """You are a careful career advisor matching one job posting against a candidate's resumes.
For EACH resume, give an honest 0-100 fit score:
 90-100 nearly every requirement is clearly met; 70-89 strong fit with minor gaps;
 50-69 plausible fit but important gaps or a different focus; below 50 poor fit
 (wrong field, seniority, required degree/clearance/citizenship not met).
reasons: 2-4 short, specific points of overlap. gaps: 0-4 short, specific missing requirements.
best_slot: the slot letter of the better resume for this job.
branch: the job family this posting belongs to, in 2-5 words (e.g. "Supply chain operations",
 "E-commerce data analyst", "Retail store management"). Jobs that would want the same tailored
 resume belong to the same branch. If one of the EXISTING BRANCHES listed for the best slot
 fits, return that name exactly; only invent a new branch when none fits.
pitch: 2-3 first-person sentences the candidate could paste into a "Why are you interested?" box, using only facts from the chosen resume. No invented experience."""


_SPACES = re.compile(r"[ \t ]+")
_BLANKS = re.compile(r"\n\s*\n+")


def compact_text(text: str) -> str:
    """Collapse runs of spaces/tabs and blank lines (page text from innerText is full of them).
    Words and line order are untouched, so nothing the posting says is lost."""
    lines = (_SPACES.sub(" ", ln).strip() for ln in text.replace("\r", "").split("\n"))
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


def extract_job(page_text: str, url: str, model: str) -> JobExtract:
    return ask_json(
        JobExtract,
        EXTRACT_SYSTEM,
        f"URL: {url}\n\nPAGE TEXT:\n{compact_text(page_text)[:60000]}",
        model=model,
        effort="low",
    )


def resumes_block(resumes: dict[str, str]) -> str:
    """The resumes, identical for every posting in a run: sent first and prompt-cached."""
    return "\n\n".join(f"<resume slot=\"{slot}\">\n{text}\n</resume>" for slot, text in resumes.items())


def score_job(job: dict, resumes: dict[str, str], model: str,
              branches: dict[str, list[str]] | None = None) -> MatchResult:
    """resumes: {"A": text, "B": text}; branches: existing tailored branches per slot."""
    branches = branches or {}
    listing = "\n".join(f"Slot {s}: {', '.join(b) if b else '(none yet)'}" for s, b in
                        ((s, branches.get(s, [])) for s in resumes))
    user = (
        f"JOB\nTitle: {job['title']}\nCompany: {job['company']}\nLocation: {job['location']}\n"
        f"Type: {job['job_type']}\n\n{job['description']}\n\nEXISTING BRANCHES\n{listing}"
    )
    result = ask_json(MatchResult, SCORE_SYSTEM, user, model=model, effort="medium",
                      cached_prefix=resumes_block(resumes))
    known = {b.lower(): b for b in branches.get(result.best_slot, [])}
    result.branch = known.get(result.branch.strip().lower(), result.branch.strip())
    for f in result.fits:
        f.score = max(0, min(100, int(f.score)))
    if result.best_slot not in resumes:
        result.best_slot = max(result.fits, key=lambda f: f.score).slot if result.fits else next(iter(resumes))
    return result


def best_fit(result: MatchResult) -> ResumeFit | None:
    for f in result.fits:
        if f.slot == result.best_slot:
            return f
    return max(result.fits, key=lambda f: f.score) if result.fits else None
