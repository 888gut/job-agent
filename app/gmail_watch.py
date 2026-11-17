"""Read Handshake emails in the Handshake Gmail account and move application statuses forward."""
import re

from sqlmodel import select

from . import google_store
from .db import Application, Job, get_settings, log, save_settings, session

RANK = {"Submitted": 0, "Confirmed": 1, "Rejected": 2, "Interview": 3, "Offer": 4}

RULES = [
    ("Offer", re.compile(r"\boffer\b", re.I)),
    ("Interview", re.compile(r"interview|schedule a (call|time)|next steps", re.I)),
    ("Rejected", re.compile(r"unfortunately|not (be )?moving forward|other candidates|position has been filled", re.I)),
    ("Confirmed", re.compile(r"application (was )?(received|submitted|sent)|thanks for applying|you applied", re.I)),
]


def classify(text: str) -> str | None:
    for status, rx in RULES:
        if rx.search(text):
            return status
    return None


def check() -> int:
    """Returns number of applications updated."""
    from googleapiclient.discovery import build

    creds = google_store.credentials("handshake_gmail")
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    seen = set(get_settings().get("gmail_seen", []))
    msgs = gmail.users().messages().list(
        userId="me", q="from:(joinhandshake.com) newer_than:21d", maxResults=50
    ).execute().get("messages", [])

    updated = 0
    with session() as s:
        apps = [(a, s.get(Job, a.job_id)) for a in s.exec(select(Application).where(Application.is_demo == False))]  # noqa: E712
        for m in msgs:
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            full = gmail.users().messages().get(
                userId="me", id=m["id"], format="metadata", metadataHeaders=["Subject"]
            ).execute()
            subject = next((h["value"] for h in full["payload"].get("headers", []) if h["name"] == "Subject"), "")
            text = f"{subject} {full.get('snippet', '')}"
            status = classify(text)
            if not status:
                continue
            for app, job in apps:
                if job and job.company and job.company.lower() in text.lower():
                    if RANK.get(status, 0) > RANK.get(app.status, 0):
                        app.status = status
                        s.add(app)
                        updated += 1
                        log(f"Email update: {job.company} - {job.title} -> {status}", "success", job.id)
                        if app.sheet_row:
                            try:
                                google_store.update_status(app.sheet_row, status)
                            except Exception as e:  # sheet update is best-effort
                                log(f"Couldn't update the Sheet row: {e}", "warn", job.id)
        s.commit()
    save_settings({"gmail_seen": list(seen)[-500:]})
    return updated
