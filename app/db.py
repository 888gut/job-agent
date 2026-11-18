"""SQLite models and small DB helpers."""
import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import event
from sqlmodel import Field, Session, SQLModel, create_engine, select

from .config import DB_URL, DEFAULT_SETTINGS

engine = create_engine(DB_URL, connect_args={"check_same_thread": False, "timeout": 30})


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    """WAL lets the dashboard read while a search/apply run is writing, instead of waiting."""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def now() -> datetime:
    return datetime.now(timezone.utc)


class Resume(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    slot: str = Field(index=True)            # "A" or "B"
    label: str = ""
    filename: str
    path: str
    pdf_path: Optional[str] = None
    text: str = ""
    master_doc_id: Optional[str] = None      # Google Doc that mirrors this resume's format
    uploaded_at: datetime = Field(default_factory=now)


class ProfileFact(SQLModel, table=True):
    """A verified fact about you that tailoring may use (beyond what's in the two resumes).

    kind "project": can be added to a resume as a short project entry (title + one bullet).
    kind "fact":    background the agent may use when rewording an existing bullet.
    Only confirmed facts are ever used.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str = "fact"                       # project | fact
    title: str = ""                          # project title (bold line)
    text: str                                # the fact itself, or the project's one resume bullet
    confirmed: bool = False
    source: str = "you"                      # you | learned in conversation
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class TailoredResume(SQLModel, table=True):
    """A one-off tailored copy of a base resume, kept as a Google Doc and reused for its branch."""
    id: Optional[int] = Field(default=None, primary_key=True)
    resume_id: int = Field(index=True)
    slot: str
    branch: str = Field(index=True)          # job family, e.g. "Supply chain operations"
    created_for_job_id: Optional[int] = None
    edits_json: str = "[]"                   # accepted edits [{paragraph, old, new, why}]
    rejected_json: str = "[]"                # edits blocked by the checks
    added_json: str = ""                     # project entry added: {"fact_id", "title", "text"}
    doc_id: Optional[str] = None
    doc_url: Optional[str] = None
    pdf_path: Optional[str] = None           # PDF exported from the Doc, uploaded to Handshake
    uses: int = 0
    is_demo: bool = False
    created_at: datetime = Field(default_factory=now)
    hs_optimized_at: Optional[datetime] = None  # last run through Handshake's resume optimizer
    full_text: str = ""                      # whole resume text when the Doc no longer matches edits_json
    superseded_by: Optional[int] = None      # a newer version of this branch replaced it


class HandshakeDoc(SQLModel, table=True):
    """A resume the agent uploaded to your Handshake Documents, so the same PDF is reused
    instead of uploaded again, and copies unused for a week can be deleted."""
    doc_id: str = Field(primary_key=True)    # Handshake's document id (/docs/<id>)
    name: str = ""
    pdf_hash: str = Field(default="", index=True)
    uploaded_at: datetime = Field(default_factory=now)
    last_used_at: datetime = Field(default_factory=now)
    sent: bool = False                       # attached to a submitted application: never deleted
    deleted: bool = False


class CoverLetter(SQLModel, table=True):
    """A cover letter written for one job, kept as a Google Doc (you can edit it there) and
    exported to PDF each time it's attached, so your edits are included."""
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(index=True)          # 0: a posting you pasted in (not one the agent found)
    company: str = ""
    title: str = ""
    posting: str = ""                        # the pasted posting text (job_id 0 only)
    prompt: str = ""                         # your instructions / the employer's question, if any
    doc_id: Optional[str] = None
    doc_url: Optional[str] = None
    pdf_path: Optional[str] = None
    text: str = ""                           # the letter as written (refreshed from each PDF export)
    rejected_json: str = "[]"                # sentences the fact check dropped [{text, problems}]
    resume_key: str = ""                     # the resume it was written from, e.g. "A" or "T12"
    created_at: datetime = Field(default_factory=now)
    exported_at: Optional[datetime] = None


class Job(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    handshake_id: str = Field(index=True, unique=True)
    url: str
    title: str = ""
    company: str = ""
    location: str = ""
    job_type: str = ""
    deadline: str = ""
    description: str = ""
    apply_type: str = "unknown"              # handshake | external | unknown
    # new | skipped | queued | applying | applied | needs_manual | failed | dismissed
    status: str = Field(default="new", index=True)
    score: Optional[int] = None
    resume_slot: Optional[str] = None
    reasons_json: str = "[]"
    gaps_json: str = "[]"
    pitch: str = ""
    branch: str = ""                         # job family this posting belongs to
    tailored_id: Optional[int] = None        # TailoredResume used for this job, if any
    note: str = ""
    is_demo: bool = False
    found_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class Application(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", index=True)
    applied_at: datetime = Field(default_factory=now)
    resume_slot: str = ""
    tailored: bool = False
    score: Optional[int] = None
    status: str = "Submitted"                # Submitted | Confirmed | Interview | Rejected | Offer
    sheet_row: Optional[int] = None
    drive_url: Optional[str] = None
    is_demo: bool = False
    email_verified: bool = False             # Gmail has a confirmation (or the user's sent application)
    email_note: str = ""                     # latest relevant email, e.g. "Nov 18: Recruiter asks for times"
    email_link: str = ""                     # Gmail link to that thread


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str


class LogEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=now, index=True)
    level: str = "info"                      # info | success | warn | error
    message: str
    job_id: Optional[int] = None


class AgentRun(SQLModel, table=True):
    """One search run or one batch of applications, for the dashboard's Search Runs page."""
    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str = Field(default="search", index=True)   # search | apply
    started_at: datetime = Field(default_factory=now, index=True)
    finished_at: Optional[datetime] = None
    status: str = "running"                  # running | done | error
    queries_json: str = "[]"                 # search terms used (search runs)
    seen: int = 0                            # postings Handshake returned
    in_area: int = 0                         # of those, within your location
    reviewed: int = 0                        # new postings sent for review
    queued: int = 0                          # jobs that made the queue
    skipped: int = 0                         # jobs scored below the minimum
    needs_you: int = 0
    errors: int = 0
    attempted: int = 0                       # apply runs: jobs tried
    applied: int = 0                         # apply runs: jobs that went through
    error: str = ""


class ClaudeCall(SQLModel, table=True):
    """Token usage of one Claude API call, for the Claude Usage page. Cost is an estimate."""
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=now, index=True)
    run_id: Optional[int] = Field(default=None, index=True)
    purpose: str = ""                        # the answer type asked for, e.g. "JobExtract"
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: float = 0.0                        # US dollars, estimated from list prices
    seconds: float = 0.0


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Tiny migration: add columns introduced after the database was first created,
    and drop columns the app no longer uses (they could block inserts)."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    ddl = col.type.compile(dialect=engine.dialect)
                    default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                    clause = f" DEFAULT {default!r}" if isinstance(default, (int, str)) and not isinstance(default, bool) else (
                        f" DEFAULT {int(default)}" if isinstance(default, bool) else "")
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}{clause}'))
            wanted = {c.name for c in table.columns}
            for name in have - wanted:
                conn.execute(text(f'ALTER TABLE "{table.name}" DROP COLUMN "{name}"'))


def session() -> Session:
    return Session(engine, expire_on_commit=False)


# ---------- settings ----------
def get_settings() -> dict:
    out = dict(DEFAULT_SETTINGS)
    with session() as s:
        for row in s.exec(select(Setting)):
            try:
                out[row.key] = json.loads(row.value)
            except json.JSONDecodeError:
                pass
    return out


def save_settings(values: dict) -> dict:
    with session() as s:
        for k, v in values.items():
            row = s.get(Setting, k)
            if row:
                row.value = json.dumps(v)
            else:
                s.add(Setting(key=k, value=json.dumps(v)))
        s.commit()
    return get_settings()


# ---------- activity log ----------
def log(message: str, level: str = "info", job_id: Optional[int] = None) -> None:
    with session() as s:
        s.add(LogEvent(message=message, level=level, job_id=job_id))
        s.commit()
    print(f"[{level}] {message}")
