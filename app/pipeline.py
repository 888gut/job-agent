"""search -> extract -> score -> tailor -> queue, and approve -> apply -> record."""
import contextlib
import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
from sqlmodel import func, select

from . import facts, google_store, handshake, layout
from .config import MASTERS, TAILORED
from .db import Application, Job, Resume, TailoredResume, get_settings, log, now, session
from .matcher import best_fit, extract_job, score_job
from .tailor import parse_layout, plan_tailoring

_run_lock = threading.Lock()
state = {"running": False, "stage": "Idle", "last_run": None, "last_error": None}


# ---------------- helpers ----------------
def current_resumes() -> dict[str, Resume]:
    with session() as s:
        out: dict[str, Resume] = {}
        for r in s.exec(select(Resume).order_by(Resume.uploaded_at)):
            out[r.slot] = r  # newest per slot wins
        return out


def applied_today() -> int:
    # "Today" is your local day (midnight to midnight on this computer), not UTC.
    start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    with session() as s:
        return s.exec(
            select(func.count()).select_from(Application)
            .where(Application.applied_at >= start, Application.is_demo == False)  # noqa: E712
        ).one()


def _hs(fn, *args, timeout: float = 300):
    return handshake.run(fn, *args).result(timeout=timeout)


def _job_dict(job: Job) -> dict:
    return {"title": job.title, "company": job.company, "location": job.location,
            "job_type": job.job_type, "description": job.description}


# ---------------- branches & tailored Google Docs ----------------
# The two base resumes are never modified. A tailored resume is a one-off Google Doc copy of the
# base resume's "master" Doc, made for one application and tagged with a job branch. A later job
# in the same branch reuses that tailored Doc instead of creating another one.

def branches_by_slot() -> dict[str, list[str]]:
    cur = current_resumes()
    out: dict[str, list[str]] = {}
    with session() as s:
        for slot, r in cur.items():
            rows = s.exec(select(TailoredResume.branch).where(
                TailoredResume.resume_id == r.id, TailoredResume.is_demo == False)).all()  # noqa: E712
            out[slot] = sorted(set(rows))
    return out


def find_branch_resume(resume_id: int, branch: str) -> TailoredResume | None:
    with session() as s:
        return s.exec(select(TailoredResume).where(
            TailoredResume.resume_id == resume_id, TailoredResume.branch == branch,
            TailoredResume.is_demo == False).order_by(TailoredResume.created_at.desc())).first()  # noqa: E712


def _link(job_id: int, tr_id: int | None) -> None:
    with session() as s:
        job = s.get(Job, job_id)
        job.tailored_id, job.updated_at = tr_id, now()
        s.add(job)
        s.commit()


_branch_locks: dict[tuple[int, str], threading.Lock] = {}
_branch_locks_guard = threading.Lock()


def _branch_lock(resume_id: int, branch: str) -> threading.Lock:
    with _branch_locks_guard:
        return _branch_locks.setdefault((resume_id, branch.strip().lower()), threading.Lock())


def tailor_job(job_id: int, fresh: bool = False) -> TailoredResume | None:
    """Give this job a tailored resume: reuse the branch's Doc, or make a new one-off."""
    settings = get_settings()
    with session() as s:
        job = s.get(Job, job_id)
    r = current_resumes().get(job.resume_slot or "A")
    if not r:
        raise RuntimeError("No resume uploaded for that slot.")
    branch = job.branch or f"{job.title}"

    # One tailoring at a time per (resume, branch): when jobs are processed in parallel, a second
    # job in the same branch waits here and then reuses the first one's Doc instead of making a
    # duplicate. Different branches still tailor in parallel.
    with _branch_lock(r.id, branch):
        if not fresh:
            existing = find_branch_resume(r.id, branch)
            if existing:
                _link(job_id, existing.id)
                log(f"Reusing the '{branch}' tailored Resume {r.slot} for {job.company}", "info", job_id)
                return existing

        others = [o for slot, o in current_resumes().items() if slot != r.slot]
        accepted, rejected, added = plan_tailoring(
            _job_dict(job), r.path, settings["model"],
            verified=facts.verified_text(r, others), facts_prompt=facts.facts_prompt(others))
        if not accepted and not added:
            _link(job_id, None)
            log(f"No safe improvements found for {job.company}; using the original Resume {r.slot}", "info", job_id)
            return None
        with session() as s:
            tr = TailoredResume(resume_id=r.id, slot=r.slot, branch=branch, created_for_job_id=job_id,
                                edits_json=json.dumps(accepted), rejected_json=json.dumps(rejected),
                                added_json=json.dumps(added) if added else "")
            s.add(tr)
            s.commit()
            s.refresh(tr)
        _link(job_id, tr.id)
    msg = f"Tailored Resume {r.slot} for {job.company} ('{branch}'): {len(accepted)} edits"
    if added:
        msg += f", added project '{added['title']}'"
    if rejected:
        msg += f", {len(rejected)} blocked by the checks"
    log(msg, "info", job_id)
    try:
        build_doc(tr.id)
    except google_store.NotConnected:
        log("The tailored Google Doc will be created once the storage Google account is connected.", "warn", job_id)
    except Exception as e:
        log(f"Couldn't create the tailored Google Doc yet: {e}", "warn", job_id)
    return tr


MASTER_PASSES = 3


def ensure_master(r: Resume, rebuild: bool = False) -> str:
    """The Google Doc that mirrors a base resume's format. Created once per uploaded resume.

    For a PDF: build the Doc, let Google render it, measure every line against the original
    PDF, adjust spacing, and rebuild (up to MASTER_PASSES times) until they line up.
    """
    if r.master_doc_id and not rebuild:
        return r.master_doc_id
    name = f"Resume {r.slot} - {r.label} (MASTER - do not edit text)"
    out_pdf = MASTERS / f"Resume{r.slot}_{r.id}_master.pdf"
    old_id = r.master_doc_id
    doc_id = None
    if Path(r.path).suffix.lower() == ".pdf":
        target = parse_layout(r.path)  # cached; a private copy, so calibrate() can adjust it
        for attempt in range(MASTER_PASSES):
            docx = layout.build_docx(target, MASTERS / f"Resume{r.slot}_{r.id}_master.docx")
            new_id = google_store.upload_as_doc(docx, name)
            if doc_id:
                google_store.trash_file(doc_id)
            doc_id = new_id
            google_store.export_pdf(doc_id, out_pdf)
            if attempt == MASTER_PASSES - 1 or not layout.calibrate(target, layout.parse(out_pdf)):
                break
    else:
        doc_id = google_store.upload_as_doc(Path(r.path), name)
        google_store.export_pdf(doc_id, out_pdf)
    if old_id and old_id != doc_id:
        try:
            google_store.trash_file(old_id)
        except Exception:
            pass
    with session() as s:
        row = s.get(Resume, r.id)
        row.master_doc_id = doc_id
        s.add(row)
        s.commit()
    r.master_doc_id = doc_id
    log(f"{'Rebuilt' if old_id else 'Created'} the Google Doc master for Resume {r.slot}.", "success")
    return doc_id


def master_pdf(r: Resume) -> Path:
    """PDF export of the master Doc (used for the format check and for .docx base resumes)."""
    out = MASTERS / f"Resume{r.slot}_{r.id}_master.pdf"
    if not out.exists():
        google_store.export_pdf(ensure_master(r), out)
    return out


def _make_tailored_doc(master_id: str, name: str, r: Resume, edits: list[dict], added: dict | None) -> str:
    """Copy the master, swap in edited text, and (optionally) add the project entry."""
    doc_id = google_store.copy_doc(master_id, name)
    missing = google_store.apply_doc_edits(doc_id, edits)
    if missing:
        log(f"{len(missing)} edit(s) couldn't be placed in the Google Doc and were skipped.", "warn")
    if added:
        lay = parse_layout(r.path)
        new_text = {e["paragraph"]: e["new"] for e in edits}
        text_of = lambda i: new_text.get(i, lay.blocks[i].text)  # noqa: E731
        secs = layout.section_of(lay)
        proj = [i for i, sec in enumerate(secs) if "PROJECT" in sec and lay.blocks[i].kind != "heading"]
        title_i = next((i for i in proj if lay.blocks[i].kind == "role"), None)
        bullet_i = next((i for i in proj if lay.blocks[i].kind == "bullet"), None)
        if title_i is None or bullet_i is None:
            log("Couldn't find a project to copy the style from; the project entry was skipped.", "warn")
        else:
            google_store.add_project(doc_id, text_of(proj[-1]), lay.blocks[title_i].left, text_of(bullet_i),
                                     added["title"], added["text"])
    return doc_id


def build_doc(tr_id: int) -> TailoredResume:
    """Copy the master Doc, swap in the tailored text, and export a PDF for Handshake."""
    with session() as s:
        tr = s.get(TailoredResume, tr_id)
        r = s.get(Resume, tr.resume_id)
        job = s.get(Job, tr.created_for_job_id) if tr.created_for_job_id else None
    pdf = TAILORED / f"tailored_{tr.id}.pdf"
    if not tr.doc_id:
        master = ensure_master(r)
        name = f"Resume {r.slot} - {tr.branch}" + (f" - {job.company}, {job.title}" if job else "")
        edits = json.loads(tr.edits_json)
        added = json.loads(tr.added_json) if tr.added_json else None
        doc_id = _make_tailored_doc(master, name, r, edits, added)
        google_store.export_pdf(doc_id, pdf)
        if added and pymupdf.open(str(pdf)).page_count > 1:
            # safety net: the project pushed it onto a second page -> rebuild without it
            google_store.trash_file(doc_id)
            doc_id = _make_tailored_doc(master, name, r, edits, None)
            google_store.export_pdf(doc_id, pdf)
            log(f"Left out the '{added['title']}' project: it would have made the resume two pages.", "warn")
            with session() as s:
                row = s.get(TailoredResume, tr_id)
                rej = json.loads(row.rejected_json)
                rej.append({"paragraph": -1, "old": "", "new": f"Add project: {added['title']}",
                            "why": added.get("why", ""), "problems": ["would make the resume two pages"]})
                row.rejected_json, row.added_json = json.dumps(rej), ""
                s.add(row)
                s.commit()
        with session() as s:
            row = s.get(TailoredResume, tr_id)
            row.doc_id, row.doc_url = doc_id, google_store.doc_url(doc_id)
            s.add(row)
            s.commit()
            tr = row
    google_store.export_pdf(tr.doc_id, pdf)
    with session() as s:
        row = s.get(TailoredResume, tr_id)
        row.pdf_path = str(pdf)
        s.add(row)
        s.commit()
        return row


# ---------------- search run ----------------
_METRO = {
    "new york": ["new york", "nyc", ", ny", "brooklyn", "manhattan", "queens", "bronx", "staten island",
                 "long island city", "jersey city", "hoboken", "newark"],
}


def location_ok(card_text: str, location: str, remote_ok: bool) -> bool:
    """Keep a search result if its card shows the wanted area, remote work, or no location at all."""
    if not location.strip():
        return True
    text = card_text.lower()
    if remote_ok and "remote" in text:
        return True
    want = location.lower()
    city = want.split(",")[0].strip()
    names = next((v for k, v in _METRO.items() if k in want), [city])
    if any(n in text for n in names):
        return True
    # a card that names no place at all: let the scoring decide
    return not re.search(r",\s*[A-Z]{2}\b|remote|hybrid|on-?site", card_text, re.I)
def run_search() -> None:
    if not _run_lock.acquire(blocking=False):
        log("A search is already running.", "warn")
        return
    state.update(running=True, stage="Starting", last_error=None)
    try:
        _run_search()
    except Exception as e:
        state["last_error"] = str(e)
        log(f"Search run stopped: {e}", "error")
    finally:
        state.update(running=False, stage="Idle", last_run=now().isoformat())
        _run_lock.release()


def _run_search() -> None:
    settings = get_settings()
    resumes = current_resumes()
    if not resumes:
        log("Upload at least one resume before running a search.", "warn")
        return
    resume_texts = {slot: r.text for slot, r in resumes.items()}

    state["stage"] = "Checking Handshake sign-in"
    if not _hs(handshake.is_logged_in):
        log("Not signed in to Handshake. Use Settings > Sign in to Handshake.", "warn")
        return

    state["stage"] = "Searching Handshake"
    per_query: list[list[tuple[str, str, str]]] = []
    seen_total = 0
    for q in settings["queries"]:
        if q.strip():
            state["stage"] = f"Searching Handshake: {q.strip()}"
            results = _hs(handshake.search, q.strip())
            seen_total += len(results)
            per_query.append([r for r in results
                              if location_ok(r[2], settings["location"], settings["remote_ok"])])
    # Interleave the searches (best result of each search first) so every kind of role is represented.
    found: dict[str, str] = {}
    for rank in range(max((len(r) for r in per_query), default=0)):
        for results in per_query:
            if rank < len(results):
                found.setdefault(results[rank][0], results[rank][1])
    with session() as s:
        known = set(s.exec(select(Job.handshake_id).where(Job.handshake_id.in_(list(found) or [""]))))
    new = [(h, u) for h, u in found.items() if h not in known][: int(settings["max_new_per_run"])]
    log(f"Found {seen_total} postings, {len(found)} in your area, reviewing {len(new)} new ones.")
    _review_postings(new, resume_texts, settings)


# Postings are read by the browser one at a time (all browser work stays on the single
# Playwright thread), while Claude extracts, scores and tailors earlier postings in parallel.
REVIEW_WORKERS = 3   # postings being worked on by Claude at the same time
READ_AHEAD = 1       # postings the browser may read ahead of the busy workers


def _needs_user(e: Exception) -> bool:
    msg = str(e).lower()
    return "sign in" in msg or "captcha" in msg or "human" in msg


class _Run:
    """Shared state for one search run's parallel reviews.

    Scoring takes turns in posting order (turn/done), so each posting sees the branches chosen
    for the postings before it, as in a one-at-a-time run; `pending` holds branches that are
    about to get a tailored resume but whose tailoring hasn't finished yet. Extraction and
    tailoring still run in parallel."""

    def __init__(self):
        self.cond = threading.Condition()
        self.next = 0                 # ticket whose turn it is to score
        self.finished: set[int] = set()
        self.pending: dict[str, set[str]] = {}

    @contextlib.contextmanager
    def turn(self, ticket: int):
        with self.cond:
            self.cond.wait_for(lambda: self.next == ticket)
        try:
            yield
        finally:
            self.done(ticket)

    def done(self, ticket: int) -> None:
        """Mark a ticket finished (scored, or failed before scoring). Safe to call twice."""
        with self.cond:
            self.finished.add(ticket)
            while self.next in self.finished:
                self.next += 1
            self.cond.notify_all()

    def branches(self) -> dict[str, list[str]]:
        out = branches_by_slot()
        for slot, names in self.pending.items():
            out[slot] = sorted(set(out.get(slot, [])) | names)
        return out


def _review_postings(new: list[tuple[str, str]], resume_texts: dict, settings: dict) -> None:
    total = len(new)
    if not total:
        return
    run = _Run()
    count = {"read": 0, "done": 0}
    count_lock = threading.Lock()
    slots = threading.BoundedSemaphore(REVIEW_WORKERS + READ_AHEAD)

    def show(reading: bool) -> None:
        with count_lock:
            read, done = count["read"], count["done"]
        if reading:
            state["stage"] = f"Reviewing posting {read} of {total}" + (f" ({done} done)" if done else "")
        else:
            state["stage"] = f"Finishing the review: {done} of {total} postings done"

    def work(ticket: int, hid: str, url: str, page_text: str) -> None:
        try:
            _analyze_posting(hid, url, page_text, resume_texts, settings, run, ticket)
        except Exception as e:
            log(f"Couldn't process posting {hid}: {e}", "error")
        finally:
            run.done(ticket)  # never hold up the postings behind this one
            with count_lock:
                count["done"] += 1
            slots.release()
            show(reading=count["read"] < total)

    pool = ThreadPoolExecutor(max_workers=REVIEW_WORKERS, thread_name_prefix="review")
    ticket = 0
    try:
        for i, (hid, url) in enumerate(new, 1):
            slots.acquire()  # don't read far ahead of the workers
            with count_lock:
                count["read"] = i
            show(reading=True)
            try:
                page_text = _hs(handshake.read_posting, url)
            except Exception as e:
                slots.release()
                log(f"Couldn't process posting {hid}: {e}", "error")
                if _needs_user(e):
                    log("Stopping this run: Handshake needs you (Settings > Sign in to Handshake).", "warn")
                    break
                continue
            pool.submit(work, ticket, hid, url, page_text)  # FIFO, so tickets start in order
            ticket += 1
        show(reading=False)
    finally:
        pool.shutdown(wait=True)  # postings already read still get finished


def _process_posting(hid: str, url: str, resume_texts: dict, settings: dict) -> None:
    """Review one posting start to finish (read, extract, score, tailor)."""
    page_text = _hs(handshake.read_posting, url)
    _analyze_posting(hid, url, page_text, resume_texts, settings)


def _analyze_posting(hid: str, url: str, page_text: str, resume_texts: dict, settings: dict,
                     run: _Run | None = None, ticket: int = 0) -> None:
    """Claude part of a review (safe to run off the browser thread). The Job row is written
    only once extraction and scoring have both succeeded, so a posting that fails (e.g. the API
    stays overloaded) leaves no half-made row behind and is simply picked up by the next run."""
    ex = extract_job(page_text, url, settings["model"])
    job = Job(handshake_id=hid, url=url, title=ex.title, company=ex.company, location=ex.location,
              job_type=ex.job_type, deadline=ex.deadline, description=ex.description, apply_type=ex.apply_type)

    with (run.turn(ticket) if run else contextlib.nullcontext()):
        match = score_job(_job_dict(job), resume_texts, settings["model"],
                          run.branches() if run else branches_by_slot())
        fit = best_fit(match)
        job.score = fit.score if fit else 0
        job.resume_slot = fit.slot if fit else None
        job.reasons_json = json.dumps(fit.reasons if fit else [])
        job.gaps_json = json.dumps(fit.gaps if fit else [])
        job.pitch = match.pitch
        job.branch = match.branch
        if job.score < int(settings["min_score"]):
            job.status = "skipped"
        else:
            job.status = "queued"
        if run and job.status == "queued" and job.score < int(settings["tailor_below"]):
            run.pending.setdefault(job.resume_slot or "A", set()).add(job.branch)
    job.updated_at = now()
    with session() as s:
        s.add(job)
        s.commit()
        s.refresh(job)

    if job.status == "skipped":
        log(f"Skipped {job.title} at {job.company} (match {job.score})", "info", job.id)
        return
    if job.score < int(settings["tailor_below"]):
        tailor_job(job.id)
    log(f"Queued {job.title} at {job.company} (match {job.score}, Resume {job.resume_slot})", "success", job.id)


# ---------------- approval ----------------
def resume_pdf_for(job: Job) -> Path | None:
    """The PDF to send: a fresh export of the tailored Doc (so your own edits in the Doc are
    included), or the untouched original PDF. Raises google_store.NotConnected if a tailored
    Doc is needed but the storage account isn't connected."""
    if job.tailored_id:
        tr = build_doc(job.tailored_id)
        return Path(tr.pdf_path)
    r = current_resumes().get(job.resume_slot or "A")
    if not r:
        return None
    if r.pdf_path and Path(r.pdf_path).exists():
        return Path(r.pdf_path)
    return master_pdf(r)  # .docx base resume: send the PDF export of its Doc


def upload_name_for(job: Job) -> str:
    """File name the employer sees, e.g. 'First_Last_Resume.pdf', from the name on the resume."""
    r = current_resumes().get(job.resume_slot or "A")
    name = ""
    try:
        if r and Path(r.path).suffix.lower() == ".pdf":
            first = parse_layout(r.path).blocks[0]
            if first.kind == "name":
                name = first.text
    except Exception:
        pass
    if not name and r:
        name = (r.text.strip().splitlines() or [""])[0]
    name = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    return f"{name}_Resume.pdf" if name and len(name) <= 40 else "Resume.pdf"


def approve(job_id: int) -> dict:
    settings = get_settings()
    if applied_today() >= int(settings["daily_cap"]):
        return {"ok": False, "message": f"Daily cap of {settings['daily_cap']} applications reached."}
    with session() as s:
        job = s.get(Job, job_id)
        if not job or job.status not in ("queued", "needs_manual"):
            return {"ok": False, "message": "This job isn't waiting for approval."}
        if job.is_demo:
            return {"ok": False, "message": "This is sample data. Clear the samples and run a real search."}
        job.status = "applying"
        s.add(job)
        s.commit()

    try:
        pdf = resume_pdf_for(job)
    except google_store.NotConnected:
        msg = ("Connect the storage Google account (Settings) so the tailored Google Doc can be created, "
               "or choose 'Use original resume'.")
        _set_status(job_id, "queued", msg)
        return {"ok": False, "message": msg}
    except Exception as e:
        _set_status(job_id, "queued", f"Couldn't prepare the resume PDF: {e}")
        return {"ok": False, "message": f"Couldn't prepare the resume PDF: {e}"}
    if not pdf:
        _set_status(job_id, "queued", "No resume uploaded for this slot.")
        return {"ok": False, "message": "No resume uploaded for this slot."}

    log(f"Applying to {job.title} at {job.company}{' (dry run)' if settings['dry_run'] else ''}", "info", job_id)
    try:
        result = _hs(handshake.apply, job.url, pdf, bool(settings["dry_run"]), upload_name_for(job))
    except FutureTimeout:
        # The browser may still be mid-application (it could even click Submit), so this is never a
        # plain failure: hand it to you to check, and stop any batch run.
        msg = "This took too long, so the agent stopped waiting. It may have been submitted: check this job on Handshake before trying again."
        _set_status(job_id, "needs_manual", msg)
        log(f"{job.company}: {msg}", "warn", job_id)
        return {"ok": False, "message": msg, "blocked": True}
    except Exception as e:
        _set_status(job_id, "failed", str(e))
        log(f"Apply failed for {job.company}: {e}", "error", job_id)
        return {"ok": False, "message": str(e)}

    if result.status == "ready":  # dry run filled everything in: keep it in the queue so it can be submitted later
        _set_status(job_id, "queued", result.note)
        log(f"{job.company}: {result.note}", "info", job_id)
        return {"ok": True, "message": result.note}
    if result.status != "applied":
        _set_status(job_id, "needs_manual", result.note)
        log(f"{job.company}: {result.note}", "warn", job_id)
        return {"ok": False, "message": result.note, "blocked": result.status == "blocked"}

    _set_status(job_id, "applied", result.note)
    record_application(job_id)
    return {"ok": True, "message": f"Applied to {job.title} at {job.company}."}


# ---------------- Start: apply to a list of jobs in one go ----------------
_batch_lock = threading.Lock()
batch = {"running": False, "total": 0, "done": 0, "current": "", "results": [], "stop": False}


def _sleep_unless_stopped(seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end and not batch["stop"]:
        time.sleep(1)


def apply_batch(job_ids: list[int]) -> None:
    """Apply to each job in order: tailored Doc if needed -> attach resume -> submit (or stop
    before Submit in dry run) -> record in the Sheet. Jobs the agent can't finish safely are
    marked 'Needs you' and the run moves on."""
    if not _batch_lock.acquire(blocking=False):
        return
    batch.update(running=True, total=len(job_ids), done=0, current="", results=[], stop=False)
    try:
        for i, jid in enumerate(job_ids):
            if batch["stop"]:
                log("Stopped the run at your request.", "warn")
                break
            with session() as s:
                job = s.get(Job, jid)
            if not job or job.status != "queued":
                batch["results"].append({"job_id": jid, "title": job.title if job else "", "company": job.company if job else "",
                                         "ok": False, "message": "No longer waiting in the queue; skipped."})
                batch["done"] = i + 1
                continue
            batch["current"] = f"{job.title} at {job.company}"
            res = approve(jid)
            batch["results"].append({"job_id": jid, "title": job.title, "company": job.company,
                                     "ok": res["ok"], "message": res["message"]})
            batch["done"] = i + 1
            if "Daily cap" in res["message"]:
                break
            if res.get("blocked"):
                log("Stopping this run: Handshake needs you (a CAPTCHA, a sign-in, or a job to check). "
                    "Sort that out, then start again.", "warn")
                break
            if i < len(job_ids) - 1:
                batch["current"] = "Pausing between applications"
                _sleep_unless_stopped(random.uniform(20, 45))  # human pace, gentler on your account
        ok = sum(1 for r in batch["results"] if r["ok"])
        log(f"Start run finished: {ok} of {len(batch['results'])} done.", "success" if ok else "warn")
    finally:
        batch.update(running=False, current="")
        _batch_lock.release()


def record_application(job_id: int) -> None:
    with session() as s:
        job = s.get(Job, job_id)
        tr = s.get(TailoredResume, job.tailored_id) if job.tailored_id else None
        app = Application(job_id=job.id, resume_slot=job.resume_slot or "",
                          tailored=tr is not None, score=job.score, drive_url=tr.doc_url if tr else None)
        s.add(app)
        if tr:
            tr.uses += 1
            s.add(tr)
        s.commit()
        s.refresh(app)
    log(f"Applied: {job.title} at {job.company}", "success", job_id)
    link = (tr.doc_url or "") if tr else f"Original Resume {job.resume_slot}"
    try:
        row = google_store.append_application(job, app, link, job.branch if tr else "")
        with session() as s:
            a = s.get(Application, app.id)
            a.sheet_row = row
            s.add(a)
            s.commit()
    except google_store.NotConnected:
        log("Storage Google account isn't connected; application saved locally only.", "warn", job_id)
    except Exception as e:
        log(f"Couldn't write to the Google Sheet: {e}", "warn", job_id)


def mark_applied_manually(job_id: int) -> None:
    _set_status(job_id, "applied", "Marked as applied by you.")
    record_application(job_id)


def _set_status(job_id: int, status: str, note: str = "") -> None:
    with session() as s:
        job = s.get(Job, job_id)
        job.status, job.note, job.updated_at = status, note, now()
        s.add(job)
        s.commit()


def next_run_hint(scheduler_job) -> str | None:
    if scheduler_job and scheduler_job.next_run_time:
        return scheduler_job.next_run_time.isoformat()
    return None
