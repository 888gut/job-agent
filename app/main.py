"""FastAPI app: JSON API + static Vista dashboard + scheduler."""
import asyncio
import json
import re
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlmodel import func, select

from . import cover_letter, demo, facts, gmail_watch, google_store, handshake, layout, live, pipeline, runs
from .config import GOOGLE_CLIENT_FILE, MASTERS, RESUMES, WEB
from .db import (Application, Job, LogEvent, ProfileFact, Resume, TailoredResume, get_settings, init_db, log,
                 now, save_settings, session)
from .llm import has_api_key
from .resumes import plain_text

scheduler = BackgroundScheduler()
SEARCH_JOB_ID = "search"


def _reschedule() -> None:
    s = get_settings()
    if scheduler.get_job(SEARCH_JOB_ID):
        scheduler.remove_job(SEARCH_JOB_ID)
    if s["schedule_enabled"]:
        scheduler.add_job(pipeline.run_search, "interval", hours=max(1, int(s["schedule_hours"])),
                          id=SEARCH_JOB_ID, max_instances=1, coalesce=True)


def _gmail_tick() -> None:
    if google_store.is_connected("handshake_gmail"):
        try:
            gmail_watch.check()
        except Exception as e:
            log(f"Gmail check failed: {e}", "warn")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    with session() as s:  # the app stopped mid-application last time: never assume either way
        for j in s.exec(select(Job).where(Job.status == "applying")):
            j.status = "needs_manual"
            j.note = "The app stopped in the middle of applying. Check this job on Handshake before trying again."
            s.add(j)
        s.commit()
    with session() as s:
        empty = s.exec(select(Job.id)).first() is None
    if empty and not get_settings().get("demo_seen"):
        demo.load()
        save_settings({"demo_seen": True})
    scheduler.start()
    _reschedule()
    scheduler.add_job(_gmail_tick, "interval", minutes=30, id="gmail", coalesce=True)
    # Weekly: each branch resume goes through Handshake's resume optimizer (each one at most every 7 days)
    scheduler.add_job(pipeline.optimize_branches, "interval", hours=24, id="hs_optimize", coalesce=True,
                      next_run_time=datetime.now() + timedelta(minutes=20))
    scheduler.add_job(pipeline.cleanup_handshake_docs, "interval", hours=24, id="hs_docs", coalesce=True,
                      next_run_time=datetime.now() + timedelta(minutes=10))
    # Keep the agent's Handshake browser running out of sight for the dashboard's Handshake tab,
    # unless you closed it there last time.
    handshake.set_keep(not get_settings().get("hs_browser_closed"))
    handshake.start_keeper()
    yield
    scheduler.shutdown(wait=False)
    handshake.set_keep(False)
    handshake.run(handshake.close)


app = FastAPI(title="Offer Ledger", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

# ---------------- local-only protection ----------------
# The dashboard holds personal data and can act on your accounts, so only this PC's own
# dashboard page may talk to it:
#  - Host must be localhost/127.0.0.1 (blocks DNS-rebinding: a website pointing its own
#    domain at 127.0.0.1 to read your data through your browser).
#  - Anything that changes state must not come from another website (blocks cross-site
#    requests that would start searches, applications, uploads, etc.).
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _hostname(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        return value.split("]")[0] + "]"
    return value.split(":")[0]


@app.middleware("http")
async def local_only(request, call_next):
    from fastapi.responses import JSONResponse
    from urllib.parse import urlsplit

    if _hostname(request.headers.get("host", "")) not in LOCAL_HOSTS:
        return JSONResponse({"detail": "This dashboard only answers on 127.0.0.1."}, status_code=403)
    if request.method not in SAFE_METHODS:
        origin = request.headers.get("origin")
        if origin and (urlsplit(origin).hostname or "") not in {h.strip("[]") for h in LOCAL_HOSTS}:
            return JSONResponse({"detail": "Requests from other websites are not allowed."}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Requests from other websites are not allowed."}, status_code=403)
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"   # always pick up dashboard updates (no stale styles)
    response.headers["X-Frame-Options"] = "DENY"            # can't be embedded in another site
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


# ---------------- serializers ----------------
_LOOKUP = object()   # "not passed: look it up" (lists pass what they already loaded)


def tailored_out(tr: TailoredResume | None, job_id: int | None = None, made_for=_LOOKUP) -> dict | None:
    if not tr:
        return None
    if made_for is _LOOKUP:
        with session() as s:
            made_for = s.get(Job, tr.created_for_job_id) if tr.created_for_job_id else None
    return {
        "id": tr.id, "slot": tr.slot, "branch": tr.branch, "doc_url": tr.doc_url, "uses": tr.uses,
        "edits": json.loads(tr.edits_json), "rejected": json.loads(tr.rejected_json),
        "added": json.loads(tr.added_json) if tr.added_json else None,
        "created_at": tr.created_at.isoformat(), "has_pdf": bool(tr.pdf_path and Path(tr.pdf_path).exists()),
        "made_for": f"{made_for.title} at {made_for.company}" if made_for else "",
        "hs_optimized_at": tr.hs_optimized_at.isoformat() if tr.hs_optimized_at else None,
        "superseded": tr.superseded_by is not None,
        "reused": job_id is not None and tr.created_for_job_id != job_id,
    }


def job_out(j: Job, tr=_LOOKUP, made_for=_LOOKUP, description: bool = True) -> dict:
    if tr is _LOOKUP:
        with session() as s:
            tr = s.get(TailoredResume, j.tailored_id) if j.tailored_id else None
    return {
        "id": j.id, "handshake_id": j.handshake_id, "url": j.url, "title": j.title, "company": j.company,
        "location": j.location, "job_type": j.job_type, "deadline": j.deadline, "apply_type": j.apply_type,
        "status": j.status, "score": j.score, "resume_slot": j.resume_slot, "branch": j.branch,
        "reasons": json.loads(j.reasons_json), "gaps": json.loads(j.gaps_json), "pitch": j.pitch,
        "tailored": tailored_out(tr, j.id, made_for),
        "cover_letter": cover_letter.out(j.id),
        "note": j.note, "is_demo": j.is_demo, "found_at": j.found_at.isoformat(),
        **({"description": j.description} if description else {}),
    }


# ---------------- overview ----------------
@app.get("/api/overview")
def overview():
    week_ago = now() - timedelta(days=7)
    with session() as s:
        apps = s.exec(select(Application)).all()
        counts = dict(s.exec(select(Job.status, func.count()).group_by(Job.status)).all())
        avg = s.exec(select(func.avg(Job.score)).where(Job.status.in_(["queued", "applied", "needs_manual"]))).one()
        jobs = {j.id: j for j in s.exec(select(Job).where(Job.id.in_([a.job_id for a in apps])))}
        facts_pending = s.exec(select(func.count()).select_from(ProfileFact).where(ProfileFact.confirmed == False)).one()  # noqa: E712
    sj = scheduler.get_job(SEARCH_JOB_ID)
    st = get_settings()
    return {
        "gmail_email": st.get("gmail_email"),
        "gmail_checked_at": st.get("gmail_checked_at"),
        "sheet_url": google_store.sheet_url(),
        "interview_alerts": [{
            "id": a.id, "company": jobs[a.job_id].company, "title": jobs[a.job_id].title,
            "status": a.status, "note": a.email_note, "link": a.email_link,
        } for a in apps if a.status in ("Interview", "Offer") and not a.is_demo and a.job_id in jobs],
        "unverified": sum(1 for a in apps if not a.is_demo and not a.email_verified),
        "applied_week": sum(1 for a in apps if a.applied_at >= week_ago),
        "applied_total": len(apps),
        "applied_today": pipeline.applied_today(),
        "awaiting": counts.get("queued", 0) + counts.get("needs_manual", 0),
        "interviews": sum(1 for a in apps if a.status in ("Interview", "Offer")),
        "avg_match": round(avg) if avg else None,
        "pipeline": {k: counts.get(k, 0) for k in ("skipped", "queued", "needs_manual", "applied")},
        "app_status": {k: sum(1 for a in apps if a.status == k)
                       for k in ("Submitted", "Confirmed", "Interview", "Rejected", "Offer")},
        "run": pipeline.state,
        "batch": pipeline.batch,
        "top_n": int(st.get("top_n", 10)),
        "next_run": sj.next_run_time.isoformat() if sj and sj.next_run_time else None,
        "demo": demo.has_demo(),
        "facts_pending": facts_pending,
        "settings": {k: st[k] for k in ("daily_cap", "dry_run", "schedule_enabled")},
    }


@app.post("/api/apply-batch")
async def apply_batch(values: dict):
    ids = [int(i) for i in values.get("job_ids", [])]
    if not ids:
        return {"ok": False, "message": "Select at least one job."}
    if pipeline.batch["running"]:
        return {"ok": False, "message": "Already applying."}
    with session() as s:
        if any((j := s.get(Job, i)) and j.is_demo for i in ids):
            return {"ok": False, "message": "Those are sample jobs. Clear the sample data and run a real search first."}
    asyncio.get_running_loop().run_in_executor(None, pipeline.apply_batch, ids)
    return {"ok": True, "message": f"Started on {len(ids)} job{'s' if len(ids) != 1 else ''}."}


@app.post("/api/apply-batch/stop")
def apply_batch_stop():
    pipeline.batch["stop"] = True
    return {"ok": True, "message": "Stopping after the current application."}


@app.post("/api/run")
async def run_now():
    if pipeline.state["running"]:
        return {"ok": False, "message": "A search is already running."}
    asyncio.get_running_loop().run_in_executor(None, pipeline.run_search)
    return {"ok": True, "message": "Search started."}


# ---------------- jobs ----------------
@app.get("/api/jobs")
def list_jobs(status: str = "", light: bool = False):
    """Jobs for the dashboard. `light` returns just what a list or search needs. Full rows leave
    out the posting text (GET /api/jobs/{id} has it) and load their tailored resumes in one go."""
    with session() as s:
        q = select(Job).order_by(Job.score.desc(), Job.found_at.desc())
        if status:
            q = q.where(Job.status.in_(status.split(",")))
        jobs = list(s.exec(q))
        if light:
            return [{"id": j.id, "url": j.url, "title": j.title, "company": j.company, "location": j.location,
                     "status": j.status, "score": j.score, "is_demo": j.is_demo} for j in jobs]
        tr_ids = {j.tailored_id for j in jobs if j.tailored_id}
        trs = {t.id: t for t in s.exec(select(TailoredResume).where(TailoredResume.id.in_(tr_ids)))} if tr_ids else {}
        by_id = {j.id: j for j in jobs}
        missing = {t.created_for_job_id for t in trs.values() if t.created_for_job_id} - by_id.keys()
        if missing:
            by_id.update({j.id: j for j in s.exec(select(Job).where(Job.id.in_(missing)))})
    out = []
    for j in jobs:
        tr = trs.get(j.tailored_id) if j.tailored_id else None
        made_for = by_id.get(tr.created_for_job_id) if tr and tr.created_for_job_id else None
        out.append(job_out(j, tr, made_for, description=False))
    return out


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    with session() as s:
        j = s.get(Job, job_id)
        if not j:
            raise HTTPException(404)
        return job_out(j)


@app.post("/api/jobs/{job_id}/approve")
async def approve(job_id: int):
    return await asyncio.get_running_loop().run_in_executor(None, pipeline.approve, job_id)


@app.post("/api/jobs/{job_id}/skip")
def skip(job_id: int):
    with session() as s:
        j = s.get(Job, job_id)
        j.status, j.updated_at = "dismissed", now()
        s.add(j)
        s.commit()
    log(f"You skipped {j.title} at {j.company}", "info", job_id)
    return {"ok": True}


# The resume for each job is picked by the scorer (originals vs. stored tailored copies);
# there is no manual resume choice. See pipeline._score_into and pipeline._needs_fresh.


@app.post("/api/jobs/{job_id}/open")
async def open_job(job_id: int):
    with session() as s:
        j = s.get(Job, job_id)
    try:
        await asyncio.wait_for(asyncio.wrap_future(handshake.run(handshake.open_for_user, j.url)), timeout=90)
        return {"ok": True, "message": "Opened the posting in your browser."}
    except Exception as e:
        return {"ok": False, "message": str(e)}


async def _employer_inputs(job_id: int):
    with session() as s:
        job = s.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    pdf = await asyncio.to_thread(pipeline.resume_pdf_for, job)
    if not pdf:
        raise HTTPException(400, "No resume uploaded for this job's slot.")
    return job, pdf, pipeline.upload_name_for(job), pipeline.contact_for(job)


@app.post("/api/jobs/{job_id}/employer-apply")
async def employer_apply(job_id: int):
    """Open the employer's own application in the dashboard's browser tab, filled in as far as is
    safe. You answer anything else and click Submit there yourself."""
    if (busy := _agent_busy()):
        raise HTTPException(409, f"The agent is busy ({busy}). Try again when it's done.")
    if handshake.login_open():
        raise HTTPException(409, "Finish signing in to Handshake first.")
    try:
        job, pdf, name, contact = await _employer_inputs(job_id)
    except google_store.NotConnected:
        raise HTTPException(400, "Connect the storage Google account (Settings) so the tailored resume can be made.")
    reuse = await asyncio.to_thread(pipeline.known_doc, pdf)
    cover = await asyncio.to_thread(_cover_for, job, pdf, name, False)
    r = await asyncio.wait_for(asyncio.wrap_future(handshake.run(handshake.employer_apply, job.url, pdf, name, contact, reuse, cover)), timeout=150)
    if r.get("doc_id"):
        await asyncio.to_thread(pipeline.note_doc_use, r["doc_id"], pdf, name, False)
    r = await _cover_followup(job, pdf, name, contact, r, cover)
    kind = r.get("kind")
    if kind == "handshake":
        pipeline._set_status(job_id, "queued", "Handshake now offers its own Quick apply for this job, so it's back in the queue.")
        return {"ok": True, "kind": kind, "message": f"{job.company} now takes applications on Handshake. It's back in the Approval Queue."}
    if kind == "two-step":
        live.select(r["target"])
        return {"ok": True, "kind": kind, "target": "", "filled": [], "resume": r.get("resume", False),
                "cover": r.get("cover", False), "cover_note": r.get("cover_note", ""),
                "message": r.get("message", ""), "job": {"id": job.id, "title": job.title, "company": job.company}}
    if kind in ("blocked", "none"):
        return {"ok": False, "kind": kind, "message": r.get("message", "Couldn't open the employer's page.")}
    if kind == "email":
        try:
            link = await asyncio.to_thread(
                gmail_watch.create_draft, r["email"], f"Application: {job.title}",
                f"Hello,\n\nI'd like to apply for the {job.title} position at {job.company}. My resume is attached.\n\n"
                f"Thank you for your consideration,\n{contact['name']}\n{contact['phone']}\n{contact['email']}\n",
                pdf, name)
            return {"ok": True, "kind": kind, "draft_url": link, "to": r["email"],
                    "message": f"This employer takes applications by email. A draft to {r['email']} with your resume attached is in your Gmail Drafts. Review it and click Send."}
        except google_store.NotConnected:
            kind = "form"  # no Gmail connected: just show the page
    live.select(r["target"])
    return {"ok": True, "kind": kind, "url": r.get("url"), "target": r["target"], "filled": r.get("filled", []),
            "resume": r.get("resume", False), "cover": r.get("cover", False), "cover_note": r.get("cover_note", ""),
            "job": {"id": job.id, "title": job.title, "company": job.company}}


def _cover_for(job, pdf, name: str, write: bool) -> dict | None:
    """The job's cover letter for an employer page or two-step box: the existing one, or (write=True,
    or "always" in Settings) a new one. None when cover letters are off or it can't be made."""
    st = get_settings()
    if not st.get("cover_letters", True):
        return None
    try:
        row = cover_letter.get(job.id)
        if row and row.doc_id:
            return cover_letter.attachment(cover_letter.export(row.id), name)
        if write or st.get("cover_letters_always"):
            return cover_letter.attachment(cover_letter.ensure(job.id, pdf), name)
    except Exception as e:
        log(f"Couldn't prepare the cover letter for {job.company}: {e}", "warn", job.id)
    return None


async def _cover_followup(job, pdf, name: str, contact: dict, r: dict, cover: dict | None) -> dict:
    """The page asked for a cover letter the agent didn't have yet: write it, then attach it."""
    if r.get("cover_doc_id") and cover:
        await asyncio.to_thread(pipeline.note_doc_use, r["cover_doc_id"], cover["pdf"], cover["name"], False)
    if not r.get("cover_wanted") or r.get("cover") or r.get("kind") not in ("two-step", "form", "account"):
        return r
    if cover is None:
        cover = await asyncio.to_thread(_cover_for, job, pdf, name, True)
    if cover is None:
        return {**r, "cover_note": "This application asks for a cover letter. Turn on cover letters in Settings, or attach one yourself."}
    if r["kind"] == "two-step":
        c = await asyncio.wait_for(asyncio.wrap_future(handshake.run(handshake.cover_into_two_step, r["target"], cover)), timeout=150)
        if c.get("cover_doc_id"):
            await asyncio.to_thread(pipeline.note_doc_use, c["cover_doc_id"], cover["pdf"], cover["name"], False)
        return {**r, **c, "cover_note": c.get("cover_problem", "")}
    f = await asyncio.wait_for(asyncio.wrap_future(handshake.run(handshake.employer_fill, r["target"], pdf, name, contact, cover)), timeout=90)
    return {**r, "cover": f.get("cover", False)}


@app.post("/api/jobs/{job_id}/employer-fill")
async def employer_fill(job_id: int, request: Request):
    if (busy := _agent_busy()):
        raise HTTPException(409, f"The agent is busy ({busy}).")
    target = (await request.json()).get("target", "")
    job, pdf, name, contact = await _employer_inputs(job_id)
    cover = await asyncio.to_thread(_cover_for, job, pdf, name, False)
    try:
        r = await asyncio.wait_for(asyncio.wrap_future(handshake.run(handshake.employer_fill, target, pdf, name, contact, cover)), timeout=90)
        if r.get("cover_wanted") and not r.get("cover") and cover is None:
            r = await _cover_followup(job, pdf, name, contact, {**r, "kind": "form"}, None)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, **r}


@app.post("/api/jobs/{job_id}/mark-applied")
async def mark_applied(job_id: int):
    await asyncio.get_running_loop().run_in_executor(None, pipeline.mark_applied_manually, job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/cover-letter")
async def write_cover_letter(job_id: int, rewrite: bool = False):
    """Write this job's cover letter now (or write a new one in place of the old)."""
    with session() as s:
        job = s.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    if job.is_demo:
        return {"ok": False, "message": "This is sample data."}
    try:
        await asyncio.to_thread(cover_letter.rewrite if rewrite else cover_letter.ensure, job_id)
    except google_store.NotConnected:
        return {"ok": False, "message": "Connect the storage Google account (Settings) so the letter can be saved as a Google Doc."}
    except Exception as e:
        return {"ok": False, "message": f"Couldn't write the cover letter: {e}"}
    return {"ok": True, "message": "Cover letter ready.", "cover_letter": cover_letter.out(job_id)}


@app.get("/api/cover-letters")
def cover_letters_list():
    """Everything the Cover Letters tab needs: the letters, the resumes a letter can be written from,
    and the jobs that could use one (with the resume the agent picked for each)."""
    options = cover_letter.resume_options()
    with session() as s:
        jobs = s.exec(select(Job).where(Job.is_demo == False, Job.status.in_(  # noqa: E712
            ["queued", "needs_manual", "applying", "applied", "skipped"])).order_by(Job.score.desc())).all()
    has = {c["job_id"] for c in cover_letter.list_all()}
    order = {"queued": 0, "needs_manual": 0, "applying": 0, "applied": 1, "skipped": 2}
    jobs = sorted(jobs, key=lambda j: (order.get(j.status, 3), -(j.score or 0)))
    st = get_settings()
    return {
        "letters": cover_letter.list_all(),
        "resumes": options,
        "jobs": [{"id": j.id, "title": j.title, "company": j.company, "status": j.status, "score": j.score,
                  "resume_key": cover_letter.resume_key(j),
                  "resume_label": cover_letter.resume_label(cover_letter.resume_key(j), options),
                  "has_letter": j.id in has} for j in jobs],
        "settings": {"cover_letters": st.get("cover_letters", True), "cover_letters_always": st.get("cover_letters_always", False)},
        "storage_connected": google_store.is_connected("storage"),
    }


@app.post("/api/cover-letters")
async def cover_letters_write(values: dict):
    """Write a letter from the Cover Letters tab: for a job the agent found (job_id) or a pasted
    posting (company, title, description), from the chosen resume, steered by an optional prompt."""
    key = str(values.get("resume_key") or "").strip() or None
    prompt = str(values.get("prompt") or "")[:2000]
    job_id = int(values.get("job_id") or 0)
    try:
        if job_id:
            with session() as s:
                job = s.get(Job, job_id)
            if not job or job.is_demo:
                return {"ok": False, "message": "That job isn't available."}
            row = await asyncio.to_thread(cover_letter.rewrite, job_id, prompt, key)
        else:
            if not key:
                return {"ok": False, "message": "Choose the resume to write from."}
            row = await asyncio.to_thread(cover_letter.create_standalone, str(values.get("company") or ""),
                                          str(values.get("title") or ""), str(values.get("description") or ""), key, prompt)
    except google_store.NotConnected:
        return {"ok": False, "message": "Connect the storage Google account (Settings) so the letter can be saved as a Google Doc."}
    except (ValueError, RuntimeError) as e:
        return {"ok": False, "message": str(e)}
    except Exception as e:
        return {"ok": False, "message": f"Couldn't write the cover letter: {e}"}
    return {"ok": True, "message": "Cover letter ready.", "letter": cover_letter.out_row(row)}


@app.delete("/api/cover-letters/{letter_id}")
def cover_letters_delete(letter_id: int):
    cover_letter.delete(letter_id)
    return {"ok": True}


@app.get("/api/cover-letters/{letter_id}/pdf")
def cover_letter_pdf(letter_id: int):
    from .db import CoverLetter

    with session() as s:
        row = s.get(CoverLetter, letter_id)
    if row and row.pdf_path and Path(row.pdf_path).exists():
        return FileResponse(row.pdf_path, filename="CoverLetter.pdf")
    raise HTTPException(404, "No PDF exported yet.")


@app.get("/api/tailored/{tr_id}/pdf")
def tailored_pdf(tr_id: int):
    with session() as s:
        tr = s.get(TailoredResume, tr_id)
    if tr and tr.pdf_path and Path(tr.pdf_path).exists():
        return FileResponse(tr.pdf_path, filename=f"Resume{tr.slot}_{tr.branch}.pdf".replace(" ", "_"))
    raise HTTPException(404, "No PDF exported yet.")


# ---------------- applications ----------------
@app.get("/api/applications")
def applications():
    with session() as s:
        rows = s.exec(select(Application, Job).where(Application.job_id == Job.id)
                      .order_by(Application.applied_at.desc())).all()
    return {
        "sheet_url": google_store.sheet_url(),
        "items": [{
            "id": a.id, "job_id": j.id, "applied_at": a.applied_at.isoformat(), "company": j.company,
            "title": j.title, "location": j.location, "url": j.url, "resume_slot": a.resume_slot,
            "tailored": a.tailored, "score": a.score, "status": a.status, "doc_url": a.drive_url,
            "branch": j.branch if a.tailored else "",
            "is_demo": a.is_demo, "email_verified": a.email_verified,
            "email_note": a.email_note, "email_link": a.email_link,
        } for a, j in rows],
    }


@app.post("/api/applications/{app_id}/status")
def set_app_status(app_id: int, status: str = Form(...)):
    with session() as s:
        a = s.get(Application, app_id)
        a.status = status
        s.add(a)
        s.commit()
    if a.sheet_row:
        try:
            google_store.update_status(a.sheet_row, status)
        except Exception as e:
            log(f"Couldn't update Sheet: {e}", "warn")
    return {"ok": True}


# ---------------- resumes ----------------
@app.post("/api/branches/optimize")
async def optimize_branches_now():
    """Run every branch resume through Handshake's resume optimizer now."""
    return await asyncio.get_running_loop().run_in_executor(None, pipeline.optimize_branches, True)


@app.get("/api/resumes")
def resumes():
    cur = pipeline.current_resumes()
    ids = [r.id for r in cur.values()]
    with session() as s:
        tailored = s.exec(select(TailoredResume).where(TailoredResume.resume_id.in_(ids or [-1]))
                          .order_by(TailoredResume.slot, TailoredResume.branch, TailoredResume.created_at.desc())).all()
        demo_tailored = s.exec(select(TailoredResume).where(TailoredResume.is_demo == True)).all()  # noqa: E712
    return {
        "slots": {slot: {"id": r.id, "label": r.label, "filename": r.filename,
                         "uploaded_at": r.uploaded_at.isoformat(), "preview": r.text[:4000],
                         "words": len(r.text.split()),
                         "master_url": google_store.doc_url(r.master_doc_id) if r.master_doc_id else None,
                         "is_pdf": Path(r.path).suffix.lower() == ".pdf"} for slot, r in cur.items()},
        "tailored": [tailored_out(t) for t in [*tailored, *demo_tailored]],
    }


# ---------------- transcript ----------------
@app.get("/api/transcript")
def transcript_get():
    t = facts.transcript()
    if not t:
        return {"uploaded": False}
    return {"uploaded": True, "filename": t["filename"], "uploaded_at": t["uploaded_at"],
            "words": len(t["text"].split()), "preview": t["text"][:4000],
            "pages": t.get("pages")}


@app.post("/api/transcript")
async def transcript_upload(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".pdf", ".docx"):
        raise HTTPException(400, "Upload the transcript as a PDF (or Word) file.")
    dest = RESUMES / f"transcript{suffix}"
    for old in RESUMES.glob("transcript.*"):
        old.unlink(missing_ok=True)
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    pages = None
    if suffix == ".pdf":
        import pymupdf

        with pymupdf.open(str(dest)) as d:
            pages = d.page_count
            text = "\n".join(p.get_text() for p in d)
    else:
        text = plain_text(dest)
    text = "\n".join(l.strip() for l in text.splitlines() if l.strip())
    if len(text.split()) < 20:
        msg = "Uploaded, but almost no text could be read (is it a scanned image?). Courses can't be used for tailoring."
    else:
        msg = "Transcript uploaded. Its courses can now be used in the coursework line."
    save_settings({"transcript": {"filename": file.filename or dest.name, "path": str(dest), "text": text,
                                  "pages": pages, "uploaded_at": now().isoformat()}})
    log(f"Uploaded transcript: {file.filename}", "success")
    return {"ok": True, "message": msg}


@app.get("/api/transcript/file")
def transcript_file():
    t = facts.transcript()
    if not t:
        raise HTTPException(404)
    return FileResponse(t["path"], filename=t["filename"])


@app.delete("/api/transcript")
def transcript_delete():
    t = facts.transcript()
    if t:
        Path(t["path"]).unlink(missing_ok=True)
    save_settings({"transcript": None})
    log("Removed the transcript.")
    return {"ok": True}


# ---------------- career facts ----------------
def fact_out(f: ProfileFact) -> dict:
    out = {"id": f.id, "kind": f.kind, "title": f.title, "text": f.text, "confirmed": f.confirmed,
           "source": f.source, "updated_at": f.updated_at.isoformat()}
    if f.kind == "project":
        checks = {}
        for slot, r in pipeline.current_resumes().items():
            if Path(r.path).suffix.lower() == ".pdf":
                ok, reason = layout.project_room(layout.parse(r.path), f.title or "x", f.text)
                checks[slot] = {"ok": ok, "reason": reason}
        out["fits"] = checks
    return out


@app.get("/api/facts")
def facts_list():
    return [fact_out(f) for f in facts.all_facts()]


@app.post("/api/facts")
def facts_add(values: dict):
    kind = values.get("kind", "fact")
    if kind not in ("fact", "project") or not str(values.get("text", "")).strip():
        raise HTTPException(400, "Write the fact (and a title for a project).")
    if kind == "project" and not str(values.get("title", "")).strip():
        raise HTTPException(400, "A project needs a title.")
    f = facts.add_fact(kind, values["text"], values.get("title", ""), confirmed=True, source="you")
    return fact_out(f)


@app.put("/api/facts/{fact_id}")
def facts_update(fact_id: int, values: dict):
    f = facts.update_fact(fact_id, **{k: values.get(k) for k in ("kind", "title", "text", "confirmed")})
    if not f:
        raise HTTPException(404)
    return fact_out(f)


@app.delete("/api/facts/{fact_id}")
def facts_delete(fact_id: int):
    facts.delete_fact(fact_id)
    return {"ok": True}


@app.post("/api/resumes/{slot}/master")
async def create_master(slot: str, rebuild: bool = False):
    r = pipeline.current_resumes().get(slot.upper())
    if not r:
        raise HTTPException(404, "No resume in that slot.")
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, pipeline.ensure_master, r, rebuild)
        await loop.run_in_executor(None, pipeline.master_pdf, r)
        return {"ok": True, "message": f"Google Doc master for Resume {r.slot} is ready."}
    except google_store.NotConnected as e:
        return {"ok": False, "message": f"{e} Connect it in Settings first."}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@app.get("/api/resumes/{slot}/preview/{which}.png")
def resume_preview(slot: str, which: str):
    """Page images for the side-by-side format check: the original PDF vs the Google Doc master."""
    r = pipeline.current_resumes().get(slot.upper())
    if not r or which not in ("original", "master"):
        raise HTTPException(404)
    if which == "original":
        if Path(r.path).suffix.lower() != ".pdf":
            raise HTTPException(404)
        src = Path(r.path)
    else:
        src = MASTERS / f"Resume{r.slot}_{r.id}_master.pdf"
        if not src.exists():
            raise HTTPException(404, "Create the Google Doc master first.")
    png = MASTERS / f"Resume{r.slot}_{r.id}_{which}.png"
    if not png.exists() or png.stat().st_mtime < src.stat().st_mtime:
        layout.render_png(src, png)
    return FileResponse(png, headers={"Cache-Control": "no-store"})


@app.post("/api/resumes/{slot}")
async def upload_resume(slot: str, file: UploadFile = File(...), label: str = Form("")):
    slot = slot.upper()
    if slot not in ("A", "B"):
        raise HTTPException(400, "Slot must be A or B.")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".docx", ".pdf"):
        raise HTTPException(400, "Upload a .docx (best) or .pdf file.")
    dest = RESUMES / f"Resume{slot}_original{suffix}"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    # The file is stored exactly as uploaded. A PDF is only read, never rewritten,
    # and it is sent to Handshake unchanged.
    text = plain_text(dest)
    with session() as s:
        s.add(Resume(slot=slot, label=label or f"Resume {slot}", filename=file.filename or dest.name,
                     path=str(dest), pdf_path=str(dest) if suffix == ".pdf" else None, text=text))
        s.commit()
    log(f"Uploaded Resume {slot}: {file.filename}", "success")
    return {"ok": True, "message": "Uploaded."}


# ---------------- settings & connections ----------------
EDITABLE = {"queries", "location", "remote_ok", "max_new_per_run", "min_score", "tailor_below", "model",
            "exclude_titles", "skip_part_time", "max_commute_minutes",
            "daily_cap", "dry_run", "schedule_enabled", "schedule_hours", "cover_letters", "cover_letters_always"}


@app.get("/api/settings")
def settings_get():
    s = get_settings()
    return {k: s[k] for k in EDITABLE}


@app.put("/api/settings")
def settings_put(values: dict):
    clean = {k: v for k, v in values.items() if k in EDITABLE}
    save_settings(clean)
    _reschedule()
    log("Settings saved.")
    return settings_get()


@app.get("/api/connections")
async def connections():
    loop = asyncio.get_running_loop()
    storage_email, gmail_email = await asyncio.gather(
        loop.run_in_executor(None, google_store.account_email, "storage"),
        loop.run_in_executor(None, google_store.account_email, "handshake_gmail"),
    )
    return {
        "anthropic": has_api_key(),
        "google_client_file": GOOGLE_CLIENT_FILE.exists(),
        "storage": {"connected": google_store.is_connected("storage"), "email": storage_email,
                    "sheet_url": google_store.sheet_url()},
        "handshake_gmail": {"connected": google_store.is_connected("handshake_gmail"), "email": gmail_email},
        "handshake": _hs_login_info(),
    }


def _hs_login_info() -> dict:
    st = get_settings()
    return {"logged_in": st.get("hs_logged_in"), "checked_at": st.get("hs_checked_at"),
            "signin_open": handshake.login_open()}


@app.post("/api/connect/{account}")
async def connect(account: str):
    if account not in google_store.ACCOUNTS:
        raise HTTPException(404)
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: google_store.credentials(account, interactive=True))
        if account == "storage":
            await asyncio.get_running_loop().run_in_executor(None, google_store.ensure_sheet)
        else:
            email = await asyncio.get_running_loop().run_in_executor(None, google_store.account_email, account)
            save_settings({"gmail_email": email})
        log(f"Connected Google account ({account}).", "success")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@app.post("/api/disconnect/{account}")
def disconnect(account: str):
    google_store.disconnect(account)
    if account == "handshake_gmail":
        save_settings({"gmail_email": None})
    return {"ok": True}


@app.post("/api/handshake/login")
async def handshake_login():
    # Signing in reopens the agent's browser profile in a normal Chrome window, which closes the
    # agent's own window, so never do it in the middle of a search or an application run.
    if pipeline.batch["running"] or pipeline.state["running"]:
        return {"ok": False, "message": "The agent is busy using Handshake right now. Sign in after it finishes (or click Stop)."}
    _keep_browser(True)
    if not handshake.login_open():  # maybe the saved session still works: check before asking you
        try:
            ok = await asyncio.wait_for(asyncio.wrap_future(handshake.run(handshake.is_logged_in)), timeout=90)
        except Exception:
            ok = False
        if ok:
            pipeline.record_handshake_login(True)
            return {"ok": True, "logged_in": True, "message": "You're already signed in to Handshake."}
    try:
        await asyncio.wait_for(asyncio.wrap_future(handshake.run(handshake.open_login)), timeout=90)
        email = get_settings().get("gmail_email") or ""
        if email:  # fill in your Handshake email on the sign-in page; the password/Google step stays yours
            asyncio.get_running_loop().run_in_executor(None, live.prefill_email, email)
            msg = f"Handshake's sign-in is open below. If it asks for your email, {email} is filled in for you. Finish signing in, then click Done signing in."
        else:
            msg = "Handshake's sign-in is open below. Sign in, then click Done signing in."
        return {"ok": True, "message": msg, "email": email}
    except Exception as e:
        log(f"Couldn't open the Handshake browser: {e}", "error")
        return {"ok": False, "message": str(e)}


@app.post("/api/handshake/login/done")
async def handshake_login_done():
    await asyncio.to_thread(handshake.finish_login)
    try:  # check with the agent's own browser that the session really took
        ok = await asyncio.wait_for(asyncio.wrap_future(handshake.run(handshake.is_logged_in)), timeout=90)
    except Exception:
        ok = False
    pipeline.record_handshake_login(ok)
    if ok:
        return {"ok": True, "logged_in": True, "message": "You're signed in to Handshake. The agent can apply now."}
    return {"ok": False, "logged_in": False,
            "message": "Handshake still isn't signed in. Click Sign in to Handshake and finish every step before clicking Done."}


@app.post("/api/handshake/popout")
async def handshake_popout():
    return {"ok": handshake.login_open(), "message": await asyncio.to_thread(handshake.pop_out)}


# ---------- Handshake tab: live view of the agent's browser ----------
def _keep_browser(keep: bool) -> None:
    handshake.set_keep(keep)
    save_settings({"hs_browser_closed": not keep})


@app.post("/api/handshake/open")
async def handshake_open():
    """Start the agent's browser (out of sight) and show Handshake in the dashboard's tab."""
    if handshake.login_open():
        return {"ok": True, "message": "The sign-in window is open."}
    _keep_browser(True)
    try:
        url = await asyncio.wait_for(asyncio.wrap_future(handshake.run(handshake.ensure_open)), timeout=90)
    except Exception as e:
        return {"ok": False, "message": f"Couldn't open the Handshake browser: {e}"}
    return {"ok": True, "url": url, "message": "The Handshake browser is running."}


@app.post("/api/handshake/close")
async def handshake_close():
    """Really close the agent's browser. It stays closed until you open it or sign in again."""
    if (busy := _agent_busy()):
        return {"ok": False, "message": f"The agent is using the browser ({busy}). Click Stop first."}
    if handshake.login_open():
        await asyncio.to_thread(handshake.finish_login)
    _keep_browser(False)
    await asyncio.wait_for(asyncio.wrap_future(handshake.run(handshake.shutdown)), timeout=60)
    # A closed browser can't vouch for the sign-in (Handshake's session may not outlive it).
    save_settings({"hs_logged_in": None})
    return {"ok": True, "message": "Closed the Handshake browser."}


def _agent_busy() -> str:
    if pipeline.batch["running"]:
        return f"Applying: {pipeline.batch.get('current') or 'starting'}"
    if pipeline.state["running"]:
        return pipeline.state.get("stage") or "Searching Handshake"
    return ""


@app.get("/api/live/status")
async def live_status():
    info = await asyncio.to_thread(live.status)
    busy = _agent_busy()
    signin = handshake.login_open()
    stored = get_settings().get("hs_logged_in")
    logged_in = None
    if info["open"]:
        # Read the sign-in from the page itself: the tab you're looking at first, then any other.
        urls = [info.get("url", "")] + [t["url"] for t in info.get("tabs", [])]
        logged_in = next((v for v in map(handshake.signed_in_from_url, urls) if v is not None), None)
        if logged_in is not None and logged_in != stored and not (signin and not logged_in):
            pipeline.record_handshake_login(logged_in)
    return {**info, "signin_open": signin, "busy": busy, "can_control": bool(info["open"]) and not busy,
            "logged_in": logged_in, "closed": not info["open"] and not handshake.keeping() and not signin}


@app.get("/api/live/frame")
async def live_frame(request: Request):
    # Only the dashboard itself may read the picture of your browser.
    if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
        raise HTTPException(403, "Not allowed.")
    try:
        jpg = await asyncio.to_thread(live.frame)
    except live.NoBrowser:
        return Response(status_code=204)
    return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/live/input")
async def live_input(request: Request):
    if (busy := _agent_busy()):
        raise HTTPException(409, f"The agent is using the browser ({busy}). You can watch, but not click, until it's done.")
    ev = await request.json()
    kind = ev.get("kind")
    try:
        if kind == "click":
            await asyncio.to_thread(live.click, float(ev["x"]), float(ev["y"]))
        elif kind == "scroll":
            await asyncio.to_thread(live.scroll, float(ev["x"]), float(ev["y"]), float(ev["dy"]))
        elif kind == "text":
            await asyncio.to_thread(live.type_text, str(ev.get("text", "")))
        elif kind == "key":
            await asyncio.to_thread(live.key, str(ev.get("key", "")), bool(ev.get("shift")))
        elif kind in ("back", "reload"):
            await asyncio.to_thread(live.go, kind)
        elif kind == "tab":
            live.select(str(ev.get("id", "")))
        else:
            raise HTTPException(400, "Unknown input.")
    except live.NoBrowser:
        raise HTTPException(409, "No Handshake browser is open right now.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/handshake/status")
async def handshake_status():
    """Confirm the Handshake sign-in with the agent's own browser. While the agent is busy or the
    sign-in window is open, answer with the last confirmed result instead of interrupting."""
    if _agent_busy() or handshake.login_open() or not handshake.keeping():
        return {**_hs_login_info(), "checked": False}
    try:
        ok = await asyncio.wait_for(asyncio.wrap_future(handshake.run(handshake.is_logged_in)), timeout=90)
    except Exception as e:
        return {**_hs_login_info(), "checked": False, "error": str(e)}
    pipeline.record_handshake_login(ok)
    return {**_hs_login_info(), "checked": True}


@app.post("/api/gmail/check")
async def gmail_check():
    try:
        r = await asyncio.get_running_loop().run_in_executor(None, gmail_watch.check)
        return {"ok": True, **r, "message": (
            f"Checked {r['scanned']} new email(s): {r['updated']} status change(s), "
            f"{r['filed']} filed to Submitted, {r['interviews']} interview email(s). "
            f"{r['unverified']} application(s) still without a confirmation email.")}
    except google_store.NotConnected:
        return {"ok": False, "message": "Connect your Handshake Gmail in Settings first."}
    except Exception as e:
        return {"ok": False, "message": str(e)}


# ---------------- GitHub (portfolio) ----------------
def _git(*args: str) -> str:
    import shutil as _sh
    import subprocess

    exe = _sh.which("git") or r"C:\Program Files\Git\cmd\git.exe"
    r = subprocess.run([exe, *args], cwd=str(WEB.parent), capture_output=True, text=True, timeout=15,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout if r.returncode == 0 else ""


_github_cache: dict = {"at": 0.0, "data": None}


@app.get("/api/github")
def github():
    """The project's commit history (local git) and its GitHub link, for the portfolio page.
    Kept for a minute: reading the git history takes a few hundred milliseconds."""
    if _github_cache["data"] is not None and time.time() - _github_cache["at"] < 60:
        return _github_cache["data"]
    data = _github_history()
    _github_cache.update(at=time.time(), data=data)
    return data


def _github_history() -> dict:
    remote = _git("config", "--get", "remote.origin.url").strip()
    web = remote[:-4] if remote.endswith(".git") else remote
    if web.startswith("git@github.com:"):
        web = "https://github.com/" + web.split(":", 1)[1]
    sep = "\x1f"
    out = _git("log", "-n", "40", f"--pretty=format:%x1e%H{sep}%h{sep}%aI{sep}%s{sep}%b", "--shortstat")
    commits = []
    for chunk in out.split("\x1e"):
        if not chunk.strip():
            continue
        parts = chunk.split(sep)
        if len(parts) < 5:
            continue
        full, short, date, subject, tail = parts[0].strip(), parts[1], parts[2], parts[3], parts[4]
        stat = re.search(r"(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?", tail)
        body = re.sub(r"\s*\d+ files? changed.*", "", tail, flags=re.S).strip()
        commits.append({
            "sha": full, "short": short, "date": date, "subject": subject, "body": body,
            "files": int(stat.group(1)) if stat else 0,
            "added": int(stat.group(2) or 0) if stat else 0, "removed": int(stat.group(3) or 0) if stat else 0,
            "url": f"{web}/commit/{full}" if web else "",
        })
    total = _git("rev-list", "--count", "HEAD").strip()
    return {"repo_url": web, "has_git": bool(_git("rev-parse", "--is-inside-work-tree").strip()),
            "total_commits": int(total) if total.isdigit() else 0, "commits": commits}


# ---------------- search runs & Claude usage ----------------
@app.get("/api/runs")
def run_history(limit: int = 50):
    return runs.list_runs(min(max(limit, 1), 200))


@app.get("/api/usage")
def claude_usage(days: int = 30):
    return runs.usage_summary(min(max(days, 1), 365))


@app.get("/api/specialists")
def specialist_list(days: int = 30):
    return runs.specialists(min(max(days, 1), 365))


# ---------------- log & demo ----------------
@app.get("/api/log")
def activity(limit: int = 200, after: int = 0):
    """Newest events first. `after`: only events newer than that id (the page's auto-refresh)."""
    with session() as s:
        q = select(LogEvent).order_by(LogEvent.id.desc()).limit(min(max(limit, 1), 1000))
        if after:
            q = q.where(LogEvent.id > after)
        rows = s.exec(q).all()
    return [{"id": r.id, "ts": r.ts.isoformat(), "level": r.level, "message": r.message, "job_id": r.job_id}
            for r in rows]


@app.post("/api/demo/clear")
def demo_clear():
    demo.clear()
    return {"ok": True}


@app.post("/api/demo/load")
def demo_load():
    demo.load()
    return {"ok": True}


app.mount("/", StaticFiles(directory=str(WEB), html=True), name="web")
