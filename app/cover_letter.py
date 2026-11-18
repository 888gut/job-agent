"""Cover letters: a short, strictly factual letter for one job, kept as a Google Doc.

What Claude may draw on: the resume this job is sent with (the exact PDF), your other resume,
your confirmed Career Facts and transcript, and the posting (for what the employer wants, never
as a source of facts about you).

Every sentence is fact-checked like a tailored resume edit (tailor.guardrail), plus rules that
matter more in prose:
  - no employer, number, tool, skill or title that isn't in the verified sources;
  - trading is described as personal / independent activity, never professional work;
  - Excel, modeling, Power BI, QuickBooks and estimating are claimed only as far as the resume
    sent with this job claims them (no "advanced", "expert", "experienced in" it doesn't say).
A sentence that fails is sent back once to be rewritten; if it still fails it is dropped.
Nothing that failed the check is ever written into the letter.

The letter lives as a Google Doc in the storage Drive folder; each time it is attached, a fresh PDF
is exported from it, so your own edits in the Doc are included (like tailored resumes).
"""
import json
import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from sqlmodel import select

from . import facts, google_store
from .config import RESUMES
from .db import CoverLetter, Job, TailoredResume, get_settings, log, now, session
from .llm import ToolReply, ask_json, run_tools
from .tailor import guardrail

LETTERS = RESUMES / "cover_letters"
LETTERS.mkdir(parents=True, exist_ok=True)


class LetterDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paragraphs: list[str]


SYSTEM = """You write a short cover letter for one job, in the candidate's own first-person voice.
Length: 250-350 words in 3-4 body paragraphs. No greeting, no sign-off, no address block (they are
added for you). Plain, warm, specific; no clichés ("I am writing to express", "perfect fit",
"passionate"), no exaggeration.

Facts: use ONLY what the RESUME BEING SENT, the other resume, the confirmed facts and the transcript
say about the candidate. The JOB POSTING tells you what the employer wants; it is never a source of
facts about the candidate. Every sentence is checked automatically and dropped if it breaks a rule:
- No employer, school, title, date, number, metric, tool, technology, certification, language or
  skill that isn't in those sources. Don't invent results, team sizes, percentages or years.
- Don't move an experience to a different employer, role or project.
- Trading was personal, independent activity: always call it
  personal or independent, never professional work, a firm, clients or a job.
- Excel, financial modeling, Power BI, QuickBooks and cost estimating: claim them only exactly as
  far as the RESUME BEING SENT does. Never add "advanced", "expert", "extensive", "strong",
  "proficient" or "experienced" to them unless that resume says so. If the job wants a skill the
  resume doesn't show, you may say the candidate is eager to learn it; don't claim it.
- Name the company and the role. Connect 2-3 real experiences to what the posting asks for.
- Keep qualifiers like "independent", "personal", "coursework", "familiarity" where they apply.
- Use straightforward sentences; each one should stand on its own as a true statement."""

RETRY = """Some sentences failed the fact check (listed below with the reason). Rewrite the letter
so that none of these claims appear; keep everything else that was fine. Do not add new facts."""


# ---------------- the fact check ----------------
_SENT = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=[\"'(\[]?[A-Z0-9])")
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
_TRADING = re.compile(r"\btrad(e|es|er|ers|ing)\b", re.I)
_PERSONAL = re.compile(r"\b(personal|personally|independent|independently|self-directed|my own|on my own|side)\b", re.I)
_PRO = re.compile(r"\b(professional|professionally|firm|clients?|desk|employer|employed|self-employed|hired|salaried|career as|business)\b", re.I)
# skills the user does not want overstated, as {label: pattern}
_SKILLS = {
    "Excel": r"\bexcel\b",
    "modeling": r"\bmodel(l)?ing\b|\bfinancial models?\b",
    "Power BI": r"\bpower\s*bi\b",
    "QuickBooks": r"\bquick\s*books\b",
    "estimating": r"\bestimat(e|es|ing|or|ors|ion)\b",
}
_LEVEL = re.compile(r"\b(advanced|expert|expertise|extensive|extensively|strong|proficient|proficiency|mastery|"
                    r"skilled|experienced|seasoned|deep|highly)\b", re.I)
_LEARNING = re.compile(r"\b(learn|learning|eager|develop|developing|grow|growing|build on|interest|interested|curious)\b", re.I)


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(text.strip()) if s.strip()]


def _plain_words(*texts: str) -> set[str]:
    """Words used in lowercase somewhere (ordinary words): those may start a sentence capitalized."""
    out = set()
    for t in texts:
        out.update(w.lower() for w in _WORD.findall(t) if w[0].islower())
    return out


_COMMON = set("""a an the i i'm i've i'd my me we our you your this that these those it its in on at as for with
to by from of and or but so while when after before during since because although though throughout
over across through working work worked building built supporting supported having being there here
what which who how why where then also both each every one most more many much some any all
thank thanks please today currently recently earlier later previously now since then first second third
finally overall ultimately additionally also besides beyond outside alongside together meanwhile instead
still yet if whether whatever given like along every combined plus similarly likewise moreover further
furthermore in-person hands-on day-to-day""".split())


def check_sentence(sentence: str, verified: str, resume_text: str, plain: set[str]) -> list[str]:
    """Problems with one letter sentence. Empty list means it may be sent."""
    problems = list(guardrail(verified, "", sentence))
    first = _WORD.search(sentence)
    if first:
        w = first.group(0)
        if w[0].isupper() and w.lower() not in plain and w.lower() not in _COMMON \
                and w.lower() not in {t.lower() for t in _WORD.findall(verified)}:
            problems.append(f"new name/term '{w}'")
    if _TRADING.search(sentence):
        if _PRO.search(sentence):
            problems.append("describes trading as professional work")
        elif not _PERSONAL.search(sentence):
            problems.append("trading must be described as personal or independent")
    low_resume = resume_text.lower()
    for label, rx in _SKILLS.items():
        if not re.search(rx, sentence, re.I):
            continue
        in_resume = re.search(rx, low_resume, re.I)
        level = _LEVEL.search(sentence)
        if not in_resume:
            if level or not _LEARNING.search(sentence):
                problems.append(f"claims {label}, which the resume sent doesn't show")
        elif level and not any(re.search(rx, ln, re.I) and level.group(0).lower() in ln.lower()
                               for ln in low_resume.splitlines()):
            problems.append(f"overstates {label} ('{level.group(0)}' isn't on the resume)")
    return sorted(set(problems))


def check_letter(paragraphs: list[str], verified: str, resume_text: str, job: dict) -> tuple[list[str], list[dict]]:
    """(paragraphs with only the sentences that pass, rejected [{text, problems}])."""
    known = f"{verified}\n{job.get('company', '')}\n{job.get('title', '')}"
    plain = _plain_words(verified, job.get("description", ""), job.get("title", ""))
    kept, rejected = [], []
    for para in paragraphs:
        ok = []
        for s in sentences(para):
            problems = check_sentence(s, known, resume_text, plain)
            if problems:
                rejected.append({"text": s, "problems": problems})
            else:
                ok.append(s)
        if ok:
            kept.append(" ".join(ok))
    return kept, rejected


PROMPT_NOTE = """THE CANDIDATE'S REQUEST is below. Follow it for focus, tone and length, and if it quotes a
question from the application (e.g. "Why do you want to work here?"), make the letter answer it.
It is NOT a source of facts about the candidate: anything it mentions that the resume being sent and
the other verified sources don't show must be left out."""


# ---------------- the Scribe sub-agent: draft, check with a tool, rewrite, submit ----------------
AGENT_TURNS = 7
MIN_WORDS = 120

AGENT_SYSTEM = SYSTEM + f"""

HOW TO WORK (you have tools)
- check_letter(paragraphs) runs the exact fact check every sentence must pass and answers "ok"
  or lists each failing sentence with the reason.
- Write the whole letter, check it, then rewrite only the failing sentences so they say what the
  sources support (or remove the claim) and check again.
- When the check says "ok", call submit_letter once with exactly those paragraphs.
- You have at most {AGENT_TURNS} turns in total, so don't spend them on checks you don't need."""

_PARAS = {"type": "array", "items": {"type": "string"}, "description": "The body paragraphs, in order."}
AGENT_TOOLS = [
    {
        "name": "check_letter",
        "description": "Fact-check the letter's body paragraphs against the resume being sent and the other verified "
                       "sources. Returns 'ok' or each failing sentence with what to fix.",
        "input_schema": {"type": "object", "properties": {"paragraphs": _PARAS},
                         "required": ["paragraphs"], "additionalProperties": False},
    },
    {
        "name": "submit_letter",
        "description": "Submit the final letter body and finish. Submit only paragraphs that passed check_letter.",
        "input_schema": {"type": "object", "properties": {"paragraphs": _PARAS},
                         "required": ["paragraphs"], "additionalProperties": False},
    },
]


def _words(paragraphs: list[str]) -> int:
    return sum(len(p.split()) for p in paragraphs)


def agent_letter(job: dict, resume_text: str, verified_all: str, *, user: str, stable: str,
                 model: str) -> list[str]:
    """Run the Scribe sub-agent; returns the submitted paragraphs. Raises on any failure (the
    caller falls back to the single call + retry)."""

    def report(paragraphs: list[str]) -> list[dict]:
        return check_letter([str(p) for p in paragraphs], verified_all, resume_text, job)[1]

    def handle(name: str, args: dict, turns_left: int) -> ToolReply:
        paragraphs = [str(p).strip() for p in args.get("paragraphs") or [] if str(p).strip()]
        if name not in ("check_letter", "submit_letter"):
            return ToolReply(f"unknown tool {name}", is_error=True)
        failing = report(paragraphs)
        lines = "\n".join(f"- \"{r['text']}\": {', '.join(r['problems'])}" for r in failing)
        if name == "check_letter":
            size = f"({_words(paragraphs)} words in {len(paragraphs)} paragraphs)"
            return ToolReply(f"ok {size}" if not failing else f"{len(failing)} sentence(s) fail {size}:\n{lines}")
        if (failing or _words(paragraphs) < MIN_WORDS or len(paragraphs) < 2) and turns_left > 0:
            why = lines or f"the letter needs at least {MIN_WORDS} words in 2+ paragraphs"
            return ToolReply(f"Not submitted. Fix these and submit again:\n{why}", is_error=True)
        return ToolReply("submitted", done=True, value=paragraphs)

    return run_tools(AGENT_SYSTEM, user, AGENT_TOOLS, handle, purpose="LetterDraft", model=model,
                     effort="medium", max_turns=AGENT_TURNS, cached_prefix=stable,
                     nudge="Please check the letter with check_letter, then finish with submit_letter.")


def write_letter(job: dict, resume_text: str, verified: str, facts_text: str, model: str,
                 prompt: str = "") -> tuple[list[str], list[dict]]:
    """Scribe drafts the letter, checks it with a tool, rewrites what fails and submits; if the
    sub-agent fails, one structured call plus one retry with feedback does it instead. Either way
    the same fact check runs again here and drops whatever still fails. Returns (paragraphs,
    rejected sentences). `prompt` (optional) steers focus and tone or carries the employer's
    question; it never counts as a verified source."""
    verified_all = f"{resume_text}\n{verified}"
    stable = (f"RESUME BEING SENT WITH THIS APPLICATION\n{resume_text}\n\n"
              f"OTHER VERIFIED SOURCES\n{facts_text or '(none beyond this resume)'}")
    user = f"JOB POSTING\nTitle: {job['title']}\nCompany: {job['company']}\n\n{job.get('description', '')}"
    if prompt.strip():
        user += f"\n\n{PROMPT_NOTE}\n<request>\n{prompt.strip()[:2000]}\n</request>"
    try:
        paragraphs = agent_letter(job, resume_text, verified_all, user=user, stable=stable, model=model)
        # final check: runs on every letter, however it was made, so nothing unsafe gets through
        kept, rejected = check_letter(paragraphs, verified_all, resume_text, job)
        if _words(kept) >= MIN_WORDS and len(kept) >= 2:
            return kept, rejected
        print("[warn] Scribe's letter was too short after the final check; using one call instead")
    except Exception as e:  # any sub-agent failure -> the original single call + retry
        print(f"[warn] Scribe sub-agent failed ({type(e).__name__}: {e}); using one call instead")
    draft = ask_json(LetterDraft, SYSTEM, user, model=model, effort="medium", cached_prefix=stable)
    kept, rejected = check_letter(draft.paragraphs, verified_all, resume_text, job)
    if rejected:
        feedback = "\n".join(f"- \"{r['text']}\" ({', '.join(r['problems'])})" for r in rejected)
        retry = ask_json(LetterDraft, SYSTEM, f"{user}\n\n{RETRY}\n{feedback}", model=model, effort="medium",
                         cached_prefix=stable)
        kept2, rejected2 = check_letter(retry.paragraphs, verified_all, resume_text, job)
        if _words(kept2) >= _words(kept) * 0.8:
            kept, rejected = kept2, rejected + rejected2
    if _words(kept) < MIN_WORDS or len(kept) < 2:
        raise RuntimeError("Couldn't write a cover letter that passes the fact check for this job.")
    return kept, rejected


# ---------------- the document ----------------
def letter_text(contact: dict, company: str, paragraphs: list[str], day: datetime | None = None) -> str:
    day = day or datetime.now()
    head = [contact.get("name", ""), " | ".join(x for x in (contact.get("email"), contact.get("phone")) if x)]
    body = [f"{day:%B} {day.day}, {day:%Y}", "", f"Dear {company} Hiring Team," if company else "Dear Hiring Manager,",
            "", *sum(([p, ""] for p in paragraphs), []), "Sincerely,", contact.get("name", "")]
    return "\n".join([h for h in head if h] + [""] + body).strip() + "\n"


def build_docx(text: str, out: Path) -> Path:
    from docx import Document
    from docx.shared import Inches, Pt

    doc = Document()
    for sec in doc.sections:
        sec.top_margin = sec.bottom_margin = Inches(1)
        sec.left_margin = sec.right_margin = Inches(1)
    style = doc.styles["Normal"]
    style.font.name, style.font.size = "Times New Roman", Pt(11)
    style.paragraph_format.space_after = Pt(0)
    lines = text.rstrip("\n").split("\n")
    for i, line in enumerate(lines):
        run = doc.add_paragraph().add_run(line)
        if i == 0:
            run.bold, run.font.size = True, Pt(14)
    doc.save(str(out))
    return out


def upload_name(resume_upload_name: str) -> str:
    """'First_Last_Resume.pdf' -> 'First_Last_CoverLetter.pdf' (Handshake shows the file name)."""
    stem = re.sub(r"_?Resume\.pdf$", "", resume_upload_name, flags=re.I)
    return f"{stem}_CoverLetter.pdf" if stem else "CoverLetter.pdf"


def _pdf_text(pdf: Path) -> str:
    import pymupdf

    with pymupdf.open(str(pdf)) as d:
        return "\n".join(p.get_text() for p in d).strip()


# ---------------- which resume a letter is written from ----------------
def resume_key(job: Job) -> str:
    """The resume the agent picked for this job: "A"/"B", or "T<id>" for a tailored branch copy."""
    return f"T{job.tailored_id}" if job.tailored_id else (job.resume_slot or "A")


def resume_options() -> list[dict]:
    """Resumes a letter can be written from: both originals and the current tailored branch copies."""
    from .pipeline import branch_heads, current_resumes

    out = [{"key": slot, "slot": slot, "kind": "original",
            "label": f"Resume {slot} (original)" + (f" · {r.label}" if r.label else "")}
           for slot, r in sorted(current_resumes().items())]
    for tr in sorted(branch_heads(), key=lambda t: (t.slot, t.branch.lower())):
        out.append({"key": f"T{tr.id}", "slot": tr.slot, "kind": "tailored",
                    "label": f"Resume {tr.slot} · {tr.branch} (tailored)"})
    return out


def resume_label(key: str, options: list[dict] | None = None) -> str:
    for o in options if options is not None else resume_options():
        if o["key"] == key:
            return o["label"]
    if key.startswith("T") and key[1:].isdigit():
        with session() as s:
            tr = s.get(TailoredResume, int(key[1:]))
        if tr:
            return f"Resume {tr.slot} · {tr.branch} (tailored, older version)"
    return f"Resume {key}"


def _slot_of(key: str) -> str:
    if key.startswith("T") and key[1:].isdigit():
        with session() as s:
            tr = s.get(TailoredResume, int(key[1:]))
        if not tr:
            raise ValueError("That tailored resume no longer exists.")
        return tr.slot
    return key


def resume_for_key(key: str) -> tuple[Path, str]:
    """(PDF to write from, base slot). A tailored copy is exported fresh from its Doc."""
    from .pipeline import build_doc, current_resumes, master_pdf

    if key.startswith("T") and key[1:].isdigit():
        tr = build_doc(int(key[1:]))
        return Path(tr.pdf_path), tr.slot
    r = current_resumes().get(key)
    if not r:
        raise ValueError(f"Resume {key} isn't uploaded.")
    return (Path(r.pdf_path) if r.pdf_path and Path(r.pdf_path).exists() else master_pdf(r)), key


def get(job_id: int) -> CoverLetter | None:
    if not job_id:
        return None
    with session() as s:
        return s.exec(select(CoverLetter).where(CoverLetter.job_id == job_id).order_by(CoverLetter.id.desc())).first()


def _sources(slot: str) -> tuple[str, str]:
    """(verified text, facts prompt) for a letter written from base resume `slot` or a copy of it."""
    from .pipeline import current_resumes

    res = current_resumes()
    base = res.get(slot) or next(iter(res.values()), None)
    others = [r for r in res.values() if base is None or r.id != base.id]
    if base is None:
        return "", facts.facts_prompt(others)
    return facts.verified_text(base, others), facts.facts_prompt(others)


def _write(job_id: int, company: str, title: str, description: str, key: str, pdf: Path, slot: str,
           prompt: str = "") -> CoverLetter:
    from . import pipeline

    resume_text = _pdf_text(pdf)
    verified, facts_text = _sources(slot)
    jd = {"title": title, "company": company, "description": description}
    paragraphs, rejected = write_letter(jd, resume_text, verified, facts_text, get_settings()["model"], prompt)
    contact = pipeline.contact_for(Job(handshake_id="-", url="", resume_slot=slot))
    text = letter_text(contact, company, paragraphs)
    with session() as s:
        row = CoverLetter(job_id=job_id, company=company, title=title, posting="" if job_id else description,
                          prompt=prompt.strip(), text=text, rejected_json=json.dumps(rejected), resume_key=key)
        s.add(row)
        s.commit()
        s.refresh(row)
    docx = build_docx(text, LETTERS / f"cover_{row.id}.docx")
    doc_id = google_store.upload_as_doc(docx, f"Cover letter - {company}, {title}")
    with session() as s:
        row = s.get(CoverLetter, row.id)
        row.doc_id, row.doc_url = doc_id, google_store.doc_url(doc_id)
        s.add(row)
        s.commit()
    if rejected:
        log(f"Cover letter for {company}: the fact check dropped {len(rejected)} sentence(s).", "info", job_id or None)
    log(f"Wrote a cover letter for {title} at {company} (from {resume_label(key)}).", "success", job_id or None)
    return export(row.id)


def create(job_id: int, resume_pdf: Path | None = None, prompt: str = "", key: str | None = None) -> CoverLetter:
    """Write a new letter for this job and save it as a Google Doc + PDF. By default it's written
    from the resume the agent picked for the job; `key` picks another (e.g. "B" or "T7")."""
    from . import pipeline

    with session() as s:
        job = s.get(Job, job_id)
    if key and key != resume_key(job):
        pdf, slot = resume_for_key(key)
    else:
        key = resume_key(job)
        pdf = Path(resume_pdf) if resume_pdf else pipeline.resume_pdf_for(job)
        slot = _slot_of(key)
    if not pdf:
        raise RuntimeError("No resume uploaded for this job's slot.")
    return _write(job_id, job.company, job.title, job.description, key, Path(pdf), slot, prompt)


def create_standalone(company: str, title: str, description: str, key: str, prompt: str = "") -> CoverLetter:
    """A letter for a posting the agent didn't find (pasted in), written from the chosen resume."""
    if not (company.strip() and title.strip() and len(description.strip()) >= 80):
        raise ValueError("Add the company, the job title and the posting text (at least a few lines).")
    pdf, slot = resume_for_key(key)
    return _write(0, company.strip(), title.strip(), description.strip(), key, pdf, slot, prompt)


def export(letter_id: int) -> CoverLetter:
    """Fresh PDF from the Google Doc (so your edits there are included)."""
    with session() as s:
        row = s.get(CoverLetter, letter_id)
    pdf = LETTERS / f"cover_{row.id}.pdf"
    google_store.export_pdf(row.doc_id, pdf)
    text = _pdf_text(pdf)
    with session() as s:
        row = s.get(CoverLetter, letter_id)
        row.pdf_path, row.exported_at = str(pdf), now()
        row.text = text or row.text
        s.add(row)
        s.commit()
        return row


def ensure(job_id: int, resume_pdf: Path | None = None) -> CoverLetter:
    """The job's letter, reused if it exists (re-exported), otherwise written now."""
    row = get(job_id)
    if row and row.doc_id:
        return export(row.id)
    if row:  # a half-made letter (e.g. Google wasn't connected): start over
        discard(job_id)
    return create(job_id, resume_pdf)


def _remove(rows: list[CoverLetter], s) -> None:
    for r in rows:
        if r.doc_id:
            try:
                google_store.trash_file(r.doc_id)
            except Exception:
                pass
        s.delete(r)
    s.commit()


def discard(job_id: int) -> None:
    """Remove the job's letter (its Doc goes to Drive's trash) so the next one is written fresh."""
    if not job_id:
        return
    with session() as s:
        _remove(list(s.exec(select(CoverLetter).where(CoverLetter.job_id == job_id))), s)


def delete(letter_id: int) -> None:
    with session() as s:
        row = s.get(CoverLetter, letter_id)
        if row:
            _remove([row], s)


def rewrite(job_id: int, prompt: str = "", key: str | None = None) -> CoverLetter:
    discard(job_id)
    return create(job_id, prompt=prompt, key=key)


# ---------------- attaching ----------------
def attachment(row: CoverLetter, resume_upload_name: str) -> dict:
    """What handshake.apply / the employer-page fill need: {pdf, name, doc_id, text}."""
    from .pipeline import known_doc

    pdf = Path(row.pdf_path)
    return {"pdf": pdf, "name": upload_name(resume_upload_name), "doc_id": known_doc(pdf), "text": row.text}


def apply_with_letter(job: Job, resume_pdf: Path, resume_upload_name: str, apply_fn, settings: dict):
    """Run apply_fn(cover) (one Handshake apply attempt), giving it this job's cover letter when there
    is one. If the application asks for a letter the agent doesn't have yet, write it and try once more.
    Returns (ApplyResult, attachment or None)."""
    from .handshake import ApplyResult

    cover = None
    on = settings.get("cover_letters", True)
    try:
        if on and settings.get("cover_letters_always"):
            cover = attachment(ensure(job.id, resume_pdf), resume_upload_name)
        elif on and (row := get(job.id)) and row.doc_id:
            cover = attachment(export(row.id), resume_upload_name)
    except Exception as e:  # no letter yet is fine here: it's written below if the form asks for one
        log(f"Couldn't prepare the cover letter for {job.company}: {e}", "warn", job.id)
    result = apply_fn(cover)
    if result.status != "needs_cover":
        return result, cover
    if not on:
        return ApplyResult("needs_manual", "This application asks for a cover letter, and writing them is off in Settings."), None
    try:
        cover = attachment(ensure(job.id, resume_pdf), resume_upload_name)
    except google_store.NotConnected:
        return ApplyResult("needs_manual", "This application asks for a cover letter. Connect the storage Google account (Settings) so the agent can write one."), None
    except Exception as e:
        return ApplyResult("needs_manual", f"This application asks for a cover letter, and the agent couldn't write one: {e}"), None
    return apply_fn(cover), cover


def note_use(job: Job, result, cover: dict | None) -> None:
    """Track the uploaded letter in your Handshake documents, so it's reused and the weekly cleanup
    covers it like the resume copies."""
    from .pipeline import note_doc_use

    if cover and getattr(result, "cover_doc_id", ""):
        note_doc_use(result.cover_doc_id, cover["pdf"], cover["name"], sent=result.status == "applied")


def out_row(row: CoverLetter, options: list[dict] | None = None, job: Job | None = None) -> dict:
    """Dashboard view of one letter."""
    if job is None and row.job_id:
        with session() as s:
            job = s.get(Job, row.job_id)
    return {"id": row.id, "job_id": row.job_id or None, "doc_url": row.doc_url,
            "company": row.company or (job.company if job else ""), "title": row.title or (job.title if job else ""),
            "job_status": job.status if job else "", "job_url": job.url if job else "",
            "has_pdf": bool(row.pdf_path and Path(row.pdf_path).exists()),
            "created_at": row.created_at.isoformat(), "words": len(row.text.split()),
            "resume_key": row.resume_key, "resume_label": resume_label(row.resume_key, options) if row.resume_key else "",
            "prompt": row.prompt, "rejected": json.loads(row.rejected_json or "[]"), "text": row.text}


def out(job_id: int) -> dict | None:
    """Dashboard view of the job's letter, if it has one."""
    row = get(job_id)
    return out_row(row) if row else None


def list_all() -> list[dict]:
    options = resume_options()
    with session() as s:
        rows = list(s.exec(select(CoverLetter).order_by(CoverLetter.id.desc())))
    return [out_row(r, options) for r in rows]
