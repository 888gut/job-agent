"""Search/apply run history and Claude token usage, for the Search Runs and Claude Usage pages.

Nothing here may break a run: every write is best-effort and failures are only printed.
"""
import json
import threading
from datetime import datetime, timedelta, timezone

from sqlmodel import func, select

from .db import AgentRun, ClaudeCall, Job, LogEvent, now, session

# ---------- prices (US$ per million tokens: input, output, cache read) ----------
# List prices; a cache write costs 1.25x input. Used for estimates only.
PRICES = {
    "claude-fable-5-2": (8.0, 40.0, 0.20),
    "claude-fable-5-1": (10.0, 50.0, 1.00),
    "claude-fable-5": (10.0, 50.0, 1.00),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
DEFAULT_PRICE = PRICES["claude-opus-5-5"]

# Which specialist each kind of answer belongs to (rows saved before the names keep working).
PURPOSES = {
    "JobExtract": "Scout",
    "MatchResult": "Matchmaker",
    "TailorPlan": "Tailor",
    "LetterDraft": "Scribe",
    "Verdicts": "Clerk",
}
_OLD_LABELS = {"Read posting": "Scout", "Score match": "Matchmaker", "Tailor resume": "Tailor",
               "Cover letter": "Scribe", "Gmail check": "Clerk"}


def _specialist(purpose: str) -> str:
    return _OLD_LABELS.get(purpose, PURPOSES.get(purpose, purpose or "Other"))


def price_for(model: str) -> tuple[float, float, float]:
    model = (model or "").lower()
    # Longest matching name first, so "claude-opus-5-5" wins over "claude-opus-5".
    for name in sorted(PRICES, key=len, reverse=True):
        if model.startswith(name):
            return PRICES[name]
    return DEFAULT_PRICE


def cost_of(model: str, inp: int, out: int, cache_read: int, cache_write: int) -> float:
    pin, pout, pread = price_for(model)
    return (inp * pin + out * pout + cache_read * pread + cache_write * pin * 1.25) / 1_000_000


# ---------- the run in progress ----------
# Search and apply runs are one at a time each (pipeline locks), so a plain module value is enough;
# worker threads of a search see the same run id.
_current: dict[str, int | None] = {"search": None, "apply": None}
_lock = threading.Lock()


def current_run_id() -> int | None:
    # A Claude call during a search belongs to the search; otherwise to an apply batch, if any.
    return _current["search"] or _current["apply"]


def start(kind: str, queries: list[str] | None = None) -> int | None:
    try:
        with session() as s:
            r = AgentRun(kind=kind, queries_json=json.dumps(queries or []))
            s.add(r)
            s.commit()
            s.refresh(r)
        with _lock:
            _current[kind] = r.id
        return r.id
    except Exception as e:  # never stop a run over bookkeeping
        print(f"[warn] couldn't record the run start: {e}")
        return None


def note(run_id: int | None, **counts) -> None:
    if not run_id:
        return
    try:
        with session() as s:
            r = s.get(AgentRun, run_id)
            if r:
                for k, v in counts.items():
                    setattr(r, k, v)
                s.add(r)
                s.commit()
    except Exception as e:
        print(f"[warn] couldn't update the run: {e}")


def finish(run_id: int | None, error: str = "", **counts) -> None:
    if not run_id:
        return
    try:
        with session() as s:
            r = s.get(AgentRun, run_id)
            if not r:
                return
            for k, v in counts.items():
                setattr(r, k, v)
            if r.kind == "search":
                # What happened to the postings this run reviewed: jobs it created, by status.
                rows = s.exec(select(Job.status, func.count()).where(Job.found_at >= r.started_at)
                              .group_by(Job.status)).all()
                by = dict(rows)
                r.queued = by.get("queued", 0) + by.get("applied", 0) + by.get("applying", 0)
                r.skipped = by.get("skipped", 0) + by.get("dismissed", 0)
                r.needs_you = by.get("needs_manual", 0)
                r.errors = s.exec(select(func.count()).select_from(LogEvent).where(
                    LogEvent.ts >= r.started_at, LogEvent.level == "error")).one()
            r.error = error or ""
            r.status = "error" if error else "done"
            r.finished_at = now()
            s.add(r)
            s.commit()
    except Exception as e:
        print(f"[warn] couldn't record the run end: {e}")
    finally:
        with _lock:
            for k, v in _current.items():
                if v == run_id:
                    _current[k] = None


# ---------- Claude calls ----------
def record_call(purpose: str, model: str, usage, seconds: float) -> None:
    """Save one call's token usage (`usage` is the API response's usage object)."""
    try:
        inp = int(getattr(usage, "input_tokens", 0) or 0)
        out = int(getattr(usage, "output_tokens", 0) or 0)
        cr = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cw = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        with session() as s:
            s.add(ClaudeCall(run_id=current_run_id(), purpose=PURPOSES.get(purpose, purpose), model=model or "",
                             input_tokens=inp, output_tokens=out, cache_read=cr, cache_write=cw,
                             cost=cost_of(model, inp, out, cr, cw), seconds=round(seconds, 2)))
            s.commit()
    except Exception as e:
        print(f"[warn] couldn't record Claude usage: {e}")


# ---------- for the dashboard ----------
def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def list_runs(limit: int = 50) -> list[dict]:
    with session() as s:
        runs = s.exec(select(AgentRun).order_by(AgentRun.started_at.desc()).limit(limit)).all()
        ids = [r.id for r in runs] or [0]
        usage = {rid: (n, c) for rid, n, c in s.exec(
            select(ClaudeCall.run_id, func.count(), func.sum(ClaudeCall.cost))
            .where(ClaudeCall.run_id.in_(ids)).group_by(ClaudeCall.run_id)).all()}
    live = set(v for v in _current.values() if v)
    out = []
    for r in runs:
        status = r.status
        if status == "running" and r.id not in live:
            status = "interrupted"  # the server stopped mid-run
        end = r.finished_at or (now() if status == "running" else None)
        calls, cost = usage.get(r.id, (0, 0.0))
        out.append({
            "id": r.id, "kind": r.kind, "status": status, "started_at": _iso(r.started_at),
            "finished_at": _iso(r.finished_at),
            "seconds": round((_aware(end) - _aware(r.started_at)).total_seconds()) if end else None,
            "queries": json.loads(r.queries_json or "[]"), "seen": r.seen, "in_area": r.in_area,
            "reviewed": r.reviewed, "queued": r.queued, "skipped": r.skipped, "needs_you": r.needs_you,
            "errors": r.errors, "attempted": r.attempted, "applied": r.applied, "error": r.error,
            "claude_calls": calls, "cost": round(cost or 0.0, 4),
        })
    return out


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def usage_summary(days: int = 30) -> dict:
    since = now() - timedelta(days=days)
    with session() as s:
        calls = s.exec(select(ClaudeCall).where(ClaudeCall.ts >= since)).all()
        total_calls = s.exec(select(func.count()).select_from(ClaudeCall)).one()
        total_cost = s.exec(select(func.sum(ClaudeCall.cost))).one() or 0.0
        searches = s.exec(select(AgentRun).where(AgentRun.kind == "search", AgentRun.started_at >= since)).all()

    def agg(rows) -> dict:
        inp = sum(c.input_tokens for c in rows)
        cr = sum(c.cache_read for c in rows)
        cw = sum(c.cache_write for c in rows)
        prompt = inp + cr + cw
        return {"calls": len(rows), "input": inp, "output": sum(c.output_tokens for c in rows),
                "cache_read": cr, "cache_write": cw, "cost": round(sum(c.cost for c in rows), 4),
                "cache_share": round(cr / prompt * 100) if prompt else None,
                "avg_seconds": round(sum(c.seconds for c in rows) / len(rows), 1) if rows else None}

    t = now()
    day_start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    by_purpose: dict[str, list] = {}
    by_model: dict[str, list] = {}
    by_day: dict[str, float] = {}
    for c in calls:
        by_purpose.setdefault(_specialist(c.purpose), []).append(c)
        by_model.setdefault(c.model or "?", []).append(c)
        d = _aware(c.ts).astimezone().strftime("%Y-%m-%d")
        by_day[d] = by_day.get(d, 0.0) + c.cost
    daily = []
    for i in range(13, -1, -1):
        d = (datetime.now().astimezone() - timedelta(days=i)).strftime("%Y-%m-%d")
        daily.append({"day": d, "cost": round(by_day.get(d, 0.0), 4)})
    run_ids = {r.id for r in searches}
    search_cost = sum(c.cost for c in calls if c.run_id in run_ids)
    queued = sum(r.queued for r in searches)
    reviewed = sum(r.reviewed for r in searches)
    return {
        "days": days,
        "today": agg([c for c in calls if _aware(c.ts) >= day_start]),
        "week": agg([c for c in calls if _aware(c.ts) >= t - timedelta(days=7)]),
        "period": agg(calls),
        "all_time": {"calls": total_calls, "cost": round(total_cost, 4)},
        "by_purpose": sorted(({"name": k, **agg(v)} for k, v in by_purpose.items()), key=lambda x: -x["cost"]),
        "by_model": sorted(({"name": k, **agg(v)} for k, v in by_model.items()), key=lambda x: -x["cost"]),
        "daily": daily,
        "per_posting": round(search_cost / reviewed, 4) if reviewed else None,
        "per_queued": round(search_cost / queued, 4) if queued else None,
        "prices": {k: {"input": v[0], "output": v[1], "cache_read": v[2]} for k, v in PRICES.items()},
    }


# ---------- the specialists (Specialists page) ----------
# The six parts of Job Agent. "claude": one structured Claude request per job; "code": plain code.
SPECIALISTS = [
    {"name": "Scout", "kind": "claude", "effort": "low", "role": "Reads each Handshake posting and pulls out the details.",
     "code": "matcher.extract_job"},
    {"name": "Matchmaker", "kind": "claude", "effort": "medium",
     "role": "Scores each job against both resumes and your tailored copies, and picks the best one.",
     "code": "matcher.score_job"},
    {"name": "Tailor", "kind": "subagent", "effort": "high",
     "role": "Rewords resume bullets for a job, using only verified facts, so it stays one page.",
     "code": "tailor.plan_tailoring"},
    {"name": "Scribe", "kind": "subagent", "effort": "medium", "role": "Writes cover letters, fact-checked against your sources.",
     "code": "cover_letter"},
    {"name": "Courier", "kind": "code", "effort": "",
     "role": "Drives the agent's Chrome on Handshake: attaches the resume and submits, at a human pace.",
     "code": "handshake.apply"},
    {"name": "Clerk", "kind": "claude", "effort": "low",
     "role": "Reads your Handshake Gmail, updates each application's status and files the emails.",
     "code": "gmail_watch"},
]


def specialists(days: int = 30) -> dict:
    from .db import Application, CoverLetter, TailoredResume, get_settings
    since = now() - timedelta(days=days)
    with session() as s:
        calls = s.exec(select(ClaudeCall).where(ClaudeCall.ts >= since)).all()
        searches = s.exec(select(AgentRun).where(AgentRun.kind == "search", AgentRun.started_at >= since)).all()
        tailored = s.exec(select(func.count()).select_from(TailoredResume)
                          .where(TailoredResume.created_at >= since, TailoredResume.is_demo == False)).one()  # noqa: E712
        letters = s.exec(select(func.count()).select_from(CoverLetter).where(CoverLetter.created_at >= since)).one()
        apps = s.exec(select(Application).where(Application.is_demo == False)).all()  # noqa: E712
    settings = get_settings()
    by: dict[str, list] = {}
    for c in calls:
        by.setdefault(_specialist(c.purpose), []).append(c)
    recent_apps = [a for a in apps if _aware(a.applied_at) >= since]
    facts = {
        "Scout": [("Postings read", sum(r.reviewed for r in searches))],
        "Matchmaker": [("Made the queue", sum(r.queued for r in searches)), ("Skipped", sum(r.skipped for r in searches))],
        "Tailor": [("Tailored copies made", tailored)],
        "Scribe": [("Letters written", letters)],
        "Courier": [("Applications sent", len(recent_apps))],
        "Clerk": [("Confirmed by email", sum(1 for a in apps if a.email_verified)),
                  ("Interviews spotted", sum(1 for a in apps if a.status in ("Interview", "Offer")))],
    }
    last = {
        "Courier": max((_aware(a.applied_at) for a in apps), default=None),
        "Clerk": settings.get("gmail_checked_at"),
    }
    out = []
    for sp in SPECIALISTS:
        rows = by.get(sp["name"], [])
        last_call = max((_aware(c.ts) for c in rows), default=None)
        lu = last.get(sp["name"]) or last_call
        out.append({**sp, "model": settings.get("model", "") if sp["kind"] != "code" else "",
                    "calls": len(rows), "cost": round(sum(c.cost for c in rows), 4),
                    "avg_seconds": round(sum(c.seconds for c in rows) / len(rows), 1) if rows else None,
                    "last_active": lu if isinstance(lu, str) else _iso(lu),
                    "facts": [{"label": k, "value": v} for k, v in facts.get(sp["name"], [])]})
    return {"days": days, "specialists": out, "runs": list_runs(15)}
