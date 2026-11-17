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
