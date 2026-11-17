"""FastAPI app: JSON API + static Vista dashboard + scheduler."""
import asyncio
import json
import re
import shutil
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import func, select

from . import demo, facts, gmail_watch, google_store, handshake, layout, pipeline
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
    with session() as s:
        empty = s.exec(select(Job.id)).first() is None
    if empty and not get_settings().get("demo_seen"):
        demo.load()
        save_settings({"demo_seen": True})
    scheduler.start()
    _reschedule()
    scheduler.add_job(_gmail_tick, "interval", minutes=30, id="gmail", coalesce=True)
    yield
    scheduler.shutdown(wait=False)
    handshake.run(handshake.close)


app = FastAPI(title="Job Agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

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
    response.headers["X-Frame-Options"] = "DENY"            # can't be embedded in another site
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


# ---------------- serializers ----------------
def tailored_out(tr: TailoredResume | None, job_id: int | None = None) -> dict | None:
    if not tr:
        return None
    with session() as s:
        made_for = s.get(Job, tr.created_for_job_id) if tr.created_for_job_id else None
    return {
        "id": tr.id, "slot": tr.slot, "branch": tr.branch, "doc_url": tr.doc_url, "uses": tr.uses,
        "edits": json.loads(tr.edits_json), "rejected": json.loads(tr.rejected_json),
        "added": json.loads(tr.added_json) if tr.added_json else None,
        "created_at": tr.created_at.isoformat(), "has_pdf": bool(tr.pdf_path and Path(tr.pdf_path).exists()),
        "made_for": f"{made_for.title} at {made_for.company}" if made_for else "",
        "reused": job_id is not None and tr.created_for_job_id != job_id,
    }


def job_out(j: Job) -> dict:
    with session() as s:
        tr = s.get(TailoredResume, j.tailored_id) if j.tailored_id else None
    return {
        "id": j.id, "handshake_id": j.handshake_id, "url": j.url, "title": j.title, "company": j.company,
        "location": j.location, "job_type": j.job_type, "deadline": j.deadline, "apply_type": j.apply_type,
        "status": j.status, "score": j.score, "resume_slot": j.resume_slot, "branch": j.branch,
        "reasons": json.loads(j.reasons_json), "gaps": json.loads(j.gaps_json), "pitch": j.pitch,
        "tailored": tailored_out(tr, j.id),
        "note": j.note, "is_demo": j.is_demo, "found_at": j.found_at.isoformat(), "description": j.description,
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
    sj = scheduler.get_job(SEARCH_JOB_ID)
    st = get_settings()
    return {
        "gmail_email": st.get("gmail_email"),
        "gmail_checked_at": st.get("gmail_checked_at"),
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
        "top_n": int(get_settings().get("top_n", 10)),
        "next_run": sj.next_run_time.isoformat() if sj and sj.next_run_time else None,
        "demo": demo.has_demo(),
        "facts_pending": sum(1 for f in facts.all_facts() if not f.confirmed),
        "settings": {k: get_settings()[k] for k in ("daily_cap", "dry_run", "schedule_enabled")},
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
def list_jobs(status: str = ""):
    with session() as s:
        q = select(Job).order_by(Job.score.desc(), Job.found_at.desc())
        if status:
            q = q.where(Job.status.in_(status.split(",")))
        return [job_out(j) for j in s.exec(q)]


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


@app.post("/api/jobs/{job_id}/tailor")
async def tailor(job_id: int, fresh: bool = False):
    with session() as s:
        j = s.get(Job, job_id)
        if j and j.is_demo:
            return {"ok": False, "message": "This is sample data."}
    try:
        tr = await asyncio.get_running_loop().run_in_executor(None, pipeline.tailor_job, job_id, fresh)
        if not tr:
            return {"ok": True, "message": "No safe improvements found; the original resume will be used."}
        return {"ok": True, "message": "Tailored resume ready." if tr.doc_url else
                "Tailored edits ready. The Google Doc is created once your storage account is connected."}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@app.post("/api/jobs/{job_id}/use-original")
def use_original(job_id: int):
    """Send the original resume for this job. The tailored Doc is kept for its branch."""
    with session() as s:
        j = s.get(Job, job_id)
        j.tailored_id = None
        s.add(j)
        s.commit()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/use-tailored/{tr_id}")
def use_tailored(job_id: int, tr_id: int):
    with session() as s:
        j, tr = s.get(Job, job_id), s.get(TailoredResume, tr_id)
        if not tr:
            raise HTTPException(404)
        j.tailored_id = tr.id
        s.add(j)
        s.commit()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/open")
async def open_job(job_id: int):
    with session() as s:
        j = s.get(Job, job_id)
    try:
        await asyncio.wait_for(asyncio.wrap_future(handshake.run(handshake.open_for_user, j.url)), timeout=90)
        return {"ok": True, "message": "Opened the posting in your browser."}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@app.post("/api/jobs/{job_id}/mark-applied")
async def mark_applied(job_id: int):
    await asyncio.get_running_loop().run_in_executor(None, pipeline.mark_applied_manually, job_id)
    return {"ok": True}


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
            "daily_cap", "dry_run", "schedule_enabled", "schedule_hours"}


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
    }


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
    try:
        await asyncio.wait_for(asyncio.wrap_future(handshake.run(handshake.open_login)), timeout=90)
        return {"ok": True, "message": "A Chrome window opened. Sign in to Handshake there, then close that window and click Check."}
    except Exception as e:
        log(f"Couldn't open the Handshake browser: {e}", "error")
        return {"ok": False, "message": str(e)}


@app.get("/api/handshake/status")
async def handshake_status():
    try:
        ok = await asyncio.wrap_future(handshake.run(handshake.is_logged_in))
        return {"logged_in": ok}
    except Exception as e:
        return {"logged_in": False, "error": str(e)}


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


@app.get("/api/github")
def github():
    """The project's commit history (local git) and its GitHub link, for the portfolio page."""
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


# ---------------- log & demo ----------------
@app.get("/api/log")
def activity(limit: int = 200):
    with session() as s:
        rows = s.exec(select(LogEvent).order_by(LogEvent.ts.desc()).limit(limit)).all()
    return [{"ts": r.ts.isoformat(), "level": r.level, "message": r.message, "job_id": r.job_id} for r in rows]


@app.post("/api/demo/clear")
def demo_clear():
    demo.clear()
    return {"ok": True}


@app.post("/api/demo/load")
def demo_load():
    demo.load()
    return {"ok": True}


app.mount("/", StaticFiles(directory=str(WEB), html=True), name="web")
