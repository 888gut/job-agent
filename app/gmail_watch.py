"""Gmail sync for the Handshake Gmail account.

One pass (`check`):
1. Search mail since the first application: anything from Handshake, or naming an applied company,
   including messages the user sent (applications sent from Gmail count as proof of submission).
2. Fetch the headers of every new message in one batched HTTP request.
3. Classify them all in one Claude call (regex fallback without an API key) and match each to an
   application.
4. Move application statuses forward, write them to the Sheet in one batch update, and file mail:
   confirmations get "Job Agent/Submitted" and leave the inbox; interviews get "Job Agent/Interviews"
   and stay in the inbox.
"""
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Literal

from pydantic import BaseModel
from sqlmodel import select

from . import google_store, llm
from .db import Application, Job, get_settings, log, save_settings, session

SUBMITTED_LABEL = "Job Agent/Submitted"
INTERVIEW_LABEL = "Job Agent/Interviews"
MAX_MESSAGES = 300

# Rank only decides between "receipt" statuses; outcome statuses follow the newest email.
RECEIPT = {"Submitted": 0, "Confirmed": 1}
OUTCOMES = {"Interview", "Rejected", "Offer"}

CATEGORY_STATUS = {
    "confirmation": "Confirmed",
    "sent_application": "Submitted",   # the user's sent mail proves it went out, not that it was received
    "interview": "Interview",
    "rejection": "Rejected",
    "offer": "Offer",
}

RULES = [
    ("Offer", re.compile(r"\boffer (letter|of employment)|pleased to offer|extend (you )?an offer", re.I)),
    ("Interview", re.compile(r"interview|schedule a (call|time|chat)|phone screen|availability to (meet|speak)", re.I)),
    ("Rejected", re.compile(r"unfortunately|not (be )?moving forward|other candidates|position has been filled", re.I)),
    ("Confirmed", re.compile(r"application (was |has been )?(received|submitted|sent)|thanks? (you )?for applying|you applied", re.I)),
]


def classify(text: str) -> str | None:
    """Regex fallback: the status an email implies, or None."""
    for status, rx in RULES:
        if rx.search(text):
            return status
    return None


class EmailVerdict(BaseModel):
    message_id: str
    application_id: int          # 0 when the email isn't about any listed application
    category: Literal["confirmation", "sent_application", "interview", "rejection", "offer", "other"]
    note: str                    # <= 15 words, e.g. "Recruiter asks for times Thu/Fri"


class Verdicts(BaseModel):
    items: list[EmailVerdict]


SYSTEM = """You sort a job seeker's emails. For each email decide which listed application it is about
(match on company and role; 0 if none or unsure) and its category:
- confirmation: the employer or Handshake confirms the application was received/submitted
- sent_application: an email the job seeker SENT that submits an application (resume attached, "applying for...")
- interview: an invitation to interview, screen, or schedule a call; or a reply asking for availability
- rejection: the employer declines or the role is closed
- offer: a job offer
- other: newsletters, job alerts, recommendations, anything else
Job-alert digests from Handshake that merely mention a company are "other". Be conservative."""


# ---------------- Gmail plumbing ----------------
def create_draft(to: str, subject: str, body: str, attachment, filename: str) -> str:
    """Save an email as a DRAFT in your Handshake Gmail with the resume attached. It is never
    sent: you review it and click Send yourself. Returns a link to your Drafts folder."""
    import base64
    from email.message import EmailMessage
    from pathlib import Path

    msg = EmailMessage()
    msg["To"], msg["Subject"] = to, subject
    msg.set_content(body)
    msg.add_attachment(Path(attachment).read_bytes(), maintype="application", subtype="pdf", filename=filename)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    _service().users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    acct = get_settings().get("gmail_email") or ""
    return f"https://mail.google.com/mail/?authuser={acct}#drafts" if acct else "https://mail.google.com/mail/#drafts"


def _service():
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=google_store.credentials("handshake_gmail"), cache_discovery=False)


def build_query(companies: list[str], since: datetime) -> str:
    names = sorted({c.strip().replace('"', "") for c in companies if c and c.strip()})
    terms = ["from:joinhandshake.com", *[f'"{n}"' for n in names]]
    return f"after:{since:%Y/%m/%d} ({' OR '.join(terms)}) -in:chats"


def _list_ids(gmail, q: str) -> list[str]:
    ids, token = [], None
    while len(ids) < MAX_MESSAGES:
        res = gmail.users().messages().list(userId="me", q=q, maxResults=100, pageToken=token).execute()
        ids += [m["id"] for m in res.get("messages", [])]
        token = res.get("nextPageToken")
        if not token:
            break
    return ids[:MAX_MESSAGES]


def _fetch_headers(gmail, ids: list[str]) -> list[dict]:
    """One batched request per 50 messages (Gmail's recommended batch size)."""
    out: list[dict] = []

    def cb(_rid, resp, err):
        if err is None and resp:
            h = {x["name"].lower(): x["value"] for x in resp.get("payload", {}).get("headers", [])}
            try:
                when = parsedate_to_datetime(h.get("date", "")).astimezone(timezone.utc)
            except Exception:
                when = datetime.fromtimestamp(int(resp.get("internalDate", "0")) / 1000, timezone.utc)
            out.append({
                "id": resp["id"], "thread": resp.get("threadId", resp["id"]),
                "labels": resp.get("labelIds", []), "from": h.get("from", ""), "to": h.get("to", ""),
                "subject": h.get("subject", ""), "snippet": resp.get("snippet", ""), "date": when,
            })

    for i in range(0, len(ids), 50):
        batch = gmail.new_batch_http_request(callback=cb)
        for mid in ids[i:i + 50]:
            batch.add(gmail.users().messages().get(
                userId="me", id=mid, format="metadata", metadataHeaders=["From", "To", "Subject", "Date"]))
        batch.execute()
    return sorted(out, key=lambda m: m["date"])


def _label_ids(gmail) -> dict[str, str]:
    have = {l["name"]: l["id"] for l in gmail.users().labels().list(userId="me").execute().get("labels", [])}
    for name in ("Job Agent", SUBMITTED_LABEL, INTERVIEW_LABEL):
        if name not in have:
            have[name] = gmail.users().labels().create(userId="me", body={
                "name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}).execute()["id"]
    return have


def _file(gmail, ids: list[str], add: str, remove_inbox: bool) -> None:
    for i in range(0, len(ids), 1000):
        body = {"ids": ids[i:i + 1000], "addLabelIds": [add]}
        if remove_inbox:
            body["removeLabelIds"] = ["INBOX"]
        gmail.users().messages().batchModify(userId="me", body=body).execute()


def message_link(email: str | None, thread_id: str) -> str:
    who = f"?authuser={email}" if email else ""
    return f"https://mail.google.com/mail/u/0/{who}#all/{thread_id}"


# ---------------- classification ----------------
def _classify_all(msgs: list[dict], apps: list[tuple[Application, Job]]) -> list[EmailVerdict]:
    if llm.has_api_key():
        listing = "\n".join(f"[{a.id}] {j.company} | {j.title}" for a, j in apps)
        emails = "\n\n".join(
            f"id={m['id']} {'SENT by job seeker' if 'SENT' in m['labels'] else 'RECEIVED'} "
            f"date={m['date']:%Y-%m-%d}\nFrom: {m['from']}\nTo: {m['to']}\nSubject: {m['subject']}\n{m['snippet']}"
            for m in msgs)
        res = llm.ask_json(Verdicts, SYSTEM, f"APPLICATIONS\n{listing}\n\nEMAILS\n{emails}",
                           effort="low", max_tokens=8000)
        return res.items
    # Fallback: regex category + company-name match.
    cat = {"Confirmed": "confirmation", "Interview": "interview", "Rejected": "rejection", "Offer": "offer"}
    out = []
    for m in msgs:
        text = f"{m['subject']} {m['snippet']}"
        status = classify(text)
        app_id = next((a.id for a, j in apps if j.company and j.company.lower() in text.lower()), 0)
        out.append(EmailVerdict(message_id=m["id"], application_id=app_id,
                                category=cat.get(status, "other") if status else "other", note=m["subject"][:80]))
    return out


def apply_verdicts(apps: list[Application], msgs: list[dict], verdicts: list[EmailVerdict],
                   email: str | None = None) -> dict:
    """Update Application rows in place (oldest email first). Returns what changed and what to file."""
    by_app = {a.id: a for a in apps}
    by_msg = {m["id"]: m for m in msgs}
    changed: dict[int, Application] = {}
    to_submitted, to_interviews = [], []
    for v in sorted(verdicts, key=lambda v: by_msg.get(v.message_id, {}).get("date", datetime.min.replace(tzinfo=timezone.utc))):
        m, a = by_msg.get(v.message_id), by_app.get(v.application_id)
        if not m or not a or v.category == "other":
            continue
        status = CATEGORY_STATUS[v.category]
        if v.category in ("confirmation", "sent_application"):
            a.email_verified = True
            to_submitted.append(m["id"])
            if v.category == "confirmation" and a.status in RECEIPT and RECEIPT[status] > RECEIPT[a.status]:
                a.status = status
        else:
            if v.category == "interview":
                to_interviews.append(m["id"])
            a.status = status                     # newest outcome wins (emails are in date order)
        a.email_note = f"{m['date']:%b %d}: {v.note}"[:200]
        a.email_link = message_link(email, m["thread"])
        changed[a.id] = a
    return {"changed": changed, "submitted": to_submitted, "interviews": to_interviews}


# ---------------- entry point ----------------
def check() -> dict:
    """Sync once. Returns counts for the UI."""
    gmail = _service()
    email = get_settings().get("gmail_email") or google_store.account_email("handshake_gmail")
    with session() as s:
        rows = s.exec(select(Application, Job).where(Application.job_id == Job.id)
                      .where(Application.is_demo == False)).all()  # noqa: E712
    if not rows:
        return {"scanned": 0, "updated": 0, "filed": 0, "interviews": 0, "unverified": 0}
    since = min(a.applied_at for a, _ in rows)
    since = (since if since.tzinfo else since.replace(tzinfo=timezone.utc)) - timedelta(days=1)
    seen = set(get_settings().get("gmail_seen", []))
    ids = [i for i in _list_ids(gmail, build_query([j.company for _, j in rows], since)) if i not in seen]
    msgs = _fetch_headers(gmail, ids) if ids else []
    verdicts = _classify_all(msgs, rows) if msgs else []
    before = {a.id: a.status for a, _ in rows}
    res = apply_verdicts([a for a, _ in rows], msgs, verdicts, email)

    if res["submitted"] or res["interviews"]:
        labels = _label_ids(gmail)
        if res["submitted"]:
            _file(gmail, res["submitted"], labels[SUBMITTED_LABEL], remove_inbox=True)
        if res["interviews"]:
            _file(gmail, res["interviews"], labels[INTERVIEW_LABEL], remove_inbox=False)

    jobs = {a.id: j for a, j in rows}
    status_changes = {}
    with session() as s:
        for a in res["changed"].values():
            s.add(a)
            if a.status != before[a.id]:
                log(f"Email update: {jobs[a.id].company} - {jobs[a.id].title} -> {a.status}",
                    "success", jobs[a.id].id)
                if a.sheet_row:
                    status_changes[a.sheet_row] = a.status
        s.commit()
    if status_changes:
        try:
            google_store.update_statuses(status_changes)
        except Exception as e:  # Sheet is a mirror; the dashboard is the source of truth
            log(f"Couldn't update the Sheet: {e}", "warn")

    save_settings({"gmail_seen": (list(seen) + [m["id"] for m in msgs])[-2000:],
                   "gmail_email": email, "gmail_checked_at": datetime.now(timezone.utc).isoformat()})
    out = {
        "scanned": len(msgs),
        "updated": sum(1 for a in res["changed"].values() if a.status != before[a.id]),
        "filed": len(res["submitted"]),
        "interviews": len(res["interviews"]),
        "unverified": sum(1 for a, _ in rows if not a.email_verified),
    }
    log(f"Gmail sync: {out['scanned']} new email(s), {out['updated']} status change(s), "
        f"{out['filed']} filed to Submitted, {out['interviews']} interview email(s).")
    return out
