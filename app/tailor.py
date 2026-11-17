"""Tailor a resume to a job with light, factual edits.

What may change (everything else is locked, including the summary, skills, titles and dates):
  - experience bullets and project bullets
  - the coursework line under Education
  - optionally one added project entry (a bold title + one bullet), taken word for word from a
    project you confirmed on the Career Facts page, and only if the page stays one page.

What Claude may draw on: this resume, your other resume, and your confirmed career facts.

Two checks then run on every edit:
  1. no-fabrication: no employer, name, number, tool or skill that isn't in those verified sources;
  2. same layout: the new text must fit in the same number of lines as the original.
"""
import copy
import re
import threading
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from . import layout as layout_mod
from .db import ProfileFact, session
from .llm import ask_json
from .resumes import paragraphs


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paragraph: int
    new_text: str
    why: str


class TailorPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    edits: list[Edit]
    add_project_id: int
    add_project_why: str


SYSTEM = """You lightly tailor a resume to a specific job WITHOUT adding anything untrue.
The resume is given as numbered paragraphs. Only paragraphs marked (editable) may change:
experience bullets, project bullets and the coursework line. Everything else is locked.

Make minor, targeted edits:
- Reword an editable line so the experience it ALREADY describes uses the job's vocabulary.
- You may make a line more specific using a VERIFIED FACT, but only a fact about that same job,
  employer or project (e.g. a detail from the other resume about the same FedEx role). Never move
  a fact to a different employer, role or project.
- The coursework line may name relevant courses that appear on the transcript.
- Keep the candidate's voice and keep each line the same length or shorter: it must still fit in
  the same number of lines. Do not change paragraphs that are already a good fit.
- Keep qualifiers that describe what kind of experience it is ("independent", "personal",
  "volunteer", "self-taught"): never make personal or side activity read like paid professional work.
- The coursework line lists only the school's own courses. Self-study platforms and online
  courses stay where they already are.
- Each edited line must read naturally, as the candidate would write it. No stiff phrasing.
- 2-6 edits is typical. Fewer is fine. If nothing should change, return no edits.

Forbidden (the edit will be rejected):
- Any employer, school, title, date, degree, certification, number/metric, tool, technology,
  language or skill that does not appear in the resume, the other resume or the confirmed facts.
- Editing locked paragraphs, moving content between paragraphs, adding or deleting paragraphs.

add_project_id: if one of the CONFIRMED PROJECTS is clearly relevant to this job and would
strengthen the application, give its id; otherwise 0. It is added word for word as written.
add_project_why: one short sentence, or "" when add_project_id is 0."""

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#./&-]*")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?%?")
_ALLOWED_CAPS = {"I", "A", "An", "The", "And", "Or", "Of", "In", "On", "For", "With", "To", "By"}


def _vocab(text: str) -> set[str]:
    return {t.lower().strip(".,;:") for t in _TOKEN.findall(text)}


def guardrail(verified: str, old: str, new: str) -> list[str]:
    """No-fabrication check against everything verified. Empty list means the edit is allowed."""
    problems: list[str] = []
    vocab = _vocab(verified)
    orig_numbers = set(_NUMBER.findall(verified))

    for n in _NUMBER.findall(new):
        if n not in orig_numbers:
            problems.append(f"new number '{n}'")

    sentence_start = True
    for m in _TOKEN.finditer(new):
        tok = m.group(0).strip(".,;:")
        known = tok.lower() in vocab
        if not known and tok and not sentence_start:
            if tok[0].isupper() and tok not in _ALLOWED_CAPS:
                problems.append(f"new name/term '{tok}'")
            elif any(c in tok for c in "+#") or (any(c.isdigit() for c in tok) and not _NUMBER.fullmatch(tok)):
                problems.append(f"new technical term '{tok}'")
        sentence_start = new[m.end(): m.end() + 2].startswith((". ", "; ", ": ")) or m.group(0).endswith((".", ";", ":"))

    if old.strip() and len(new) > max(len(old) * 1.4, len(old) + 40):
        problems.append("much longer than the original")
    if not new.strip():
        problems.append("empty text")
    return sorted(set(problems))


def editable_indexes(lay: "layout_mod.Layout") -> set[int]:
    """Experience/project bullets and the coursework line under Education."""
    secs = layout_mod.section_of(lay)
    out = set()
    for i, b in enumerate(lay.blocks):
        if b.kind == "bullet":
            out.add(i)
        elif b.kind == "text" and "EDUCATION" in secs[i]:
            out.add(i)
    return out


_parse_cache: dict[tuple[str, int, int], "layout_mod.Layout"] = {}
_parse_lock = threading.Lock()


def parse_layout(pdf_path: str | Path) -> "layout_mod.Layout":
    """layout.parse() with a small cache keyed by path + mtime + size (a re-upload or edit
    invalidates it). Returns a private deep copy, because callers such as calibrate() mutate it."""
    p = Path(pdf_path)
    st = p.stat()
    key = (str(p.resolve()), st.st_mtime_ns, st.st_size)
    with _parse_lock:
        lay = _parse_cache.get(key)
    if lay is None:
        lay = layout_mod.parse(p)
        with _parse_lock:
            if len(_parse_cache) >= 8:
                _parse_cache.clear()
            _parse_cache[key] = lay
    return copy.deepcopy(lay)


def resume_structure(resume_path: str):
    """(paragraph texts, editable index set, Layout or None)."""
    if Path(resume_path).suffix.lower() == ".pdf":
        lay = parse_layout(resume_path)
        return [b.text for b in lay.blocks], editable_indexes(lay), lay
    paras = paragraphs(resume_path)
    return paras, {i for i, t in enumerate(paras) if t.strip()}, None


def plan_tailoring(job: dict, resume_path: str, model: str, verified: str = "",
                   facts_prompt: str = "") -> tuple[list[dict], list[dict], dict | None]:
    """Returns (accepted edits, rejected edits, added project or None)."""
    paras, editable, lay = resume_structure(resume_path)
    verified = "\n".join(paras) + "\n" + verified
    numbered = "\n".join(
        f"[{i}]{' (editable)' if i in editable else ' (locked)'} {t}" for i, t in enumerate(paras) if t.strip()
    )
    # resume + facts are the same for every job tailored from this resume: send them first,
    # prompt-cached, and the job last
    stable = (
        f"RESUME BEING TAILORED (numbered paragraphs)\n{numbered}\n\n"
        f"VERIFIED FACTS (true; use only as described)\n{facts_prompt or '(none beyond this resume)'}"
    )
    user = f"JOB\nTitle: {job['title']}\nCompany: {job['company']}\n\n{job['description']}"
    plan = ask_json(TailorPlan, SYSTEM, user, model=model, effort="high", cached_prefix=stable)

    accepted, rejected, seen = [], [], set()
    for e in plan.edits:
        if e.paragraph < 0 or e.paragraph >= len(paras) or e.paragraph in seen:
            continue
        old, new = paras[e.paragraph], e.new_text.strip()
        if not old.strip() or old.strip() == new:
            continue
        item = {"paragraph": e.paragraph, "old": old, "new": new, "why": e.why}
        problems = guardrail(verified, old, new)
        if e.paragraph not in editable:
            problems.append("that line is locked (only bullets and coursework may change)")
        if lay is not None:
            ok, old_n, new_n = layout_mod.fits(lay, lay.blocks[e.paragraph], new)
            if not ok:
                problems.append(f"needs {new_n} lines instead of {old_n}, which would change the layout")
        if problems:
            rejected.append({**item, "problems": sorted(set(problems))})
        else:
            accepted.append(item)
            seen.add(e.paragraph)

    added = None
    if plan.add_project_id:
        with session() as s:
            fact = s.get(ProfileFact, plan.add_project_id)
        if not fact or not fact.confirmed or fact.kind != "project":
            pass  # only confirmed projects, word for word
        elif lay is None:
            rejected.append({"paragraph": -1, "old": "", "new": f"{fact.title}: {fact.text}",
                             "why": plan.add_project_why, "problems": ["only supported for PDF resumes"]})
        else:
            ok, reason = layout_mod.project_room(lay, fact.title, fact.text)
            if ok:
                added = {"fact_id": fact.id, "title": fact.title, "text": fact.text, "why": plan.add_project_why}
            else:
                rejected.append({"paragraph": -1, "old": "", "new": f"Add project: {fact.title}",
                                 "why": plan.add_project_why, "problems": [reason]})
    return accepted, rejected, added
