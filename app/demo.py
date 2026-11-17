"""Sample data so the dashboard has something to show before the first real run."""
import json
import random
from datetime import timedelta

from sqlmodel import delete, select

from .db import Application, Job, LogEvent, TailoredResume, now, session

# title, company, location, score, slot, status, branch (None = original resume)
SAMPLE_JOBS = [
    ("Software Engineering Intern", "Northwind Labs", "Austin, TX", 91, "A", "queued", None),
    ("Data Analyst, New Grad", "Contoso Health", "Remote", 74, "B", "queued", "E-commerce data analyst"),
    ("Product Analyst Intern", "Fabrikam", "Chicago, IL", 66, "B", "queued", "E-commerce data analyst"),
    ("Junior Backend Developer", "Tailspin Toys", "Denver, CO", 83, "A", "applied", None),
    ("Business Intelligence Intern", "Litware", "Remote", 78, "B", "applied", "E-commerce data analyst"),
    ("Logistics Coordinator", "Adventure Works", "Newark, NJ", 72, "A", "applied", "Supply chain operations"),
    ("Machine Learning Intern", "Proseware", "Boston, MA", 88, "A", "applied", None),
    ("Warehouse Operations Associate", "Wide World Importers", "Bronx, NY", 69, "A", "applied", "Supply chain operations"),
    ("Marketing Coordinator", "Blue Yonder Airlines", "Phoenix, AZ", 38, "B", "skipped", None),
    ("IT Support Technician", "Coho Winery", "Napa, CA", 61, "A", "needs_manual", None),
]
APP_STATUSES = ["Submitted", "Confirmed", "Interview", "Confirmed", "Rejected"]
SAMPLE_EDITS = {
    "E-commerce data analyst": [
        {"paragraph": 15, "old": "Analyzed regional sales data to plan weekly restocking for 12 stores.",
         "new": "Tracked weekly sell-through across 12 stores to plan restocking from regional sales data.",
         "why": "Leads with metrics tracking, which the posting emphasizes."},
        {"paragraph": 24, "old": "Technical: Excel (pivot tables), Python, SQL, Tableau",
         "new": "Technical: SQL, Excel (pivot tables), Python, Tableau",
         "why": "Puts SQL first, as the job lists it first."},
    ],
    "Supply chain operations": [
        {"paragraph": 14, "old": "Coordinated weekend receiving shifts for a 4-person dock team.",
         "new": "Led weekend receiving for a 4-person dock team, cutting unload time by scheduling deliveries.",
         "why": "Emphasizes workflow leadership for an operations role."},
    ],
}


def load() -> None:
    clear()
    t0 = now()
    with session() as s:
        branch_ids: dict[str, int] = {}
        for i, (title, company, loc, score, slot, status, branch) in enumerate(SAMPLE_JOBS):
            job = Job(
                handshake_id=f"demo-{i}", url="https://app.joinhandshake.com/job-search", title=title,
                company=company, location=loc, job_type="Internship" if "Intern" in title else "Full-time",
                description="Sample posting used to preview the dashboard.", apply_type="handshake",
                status=status, score=score, resume_slot=slot, branch=branch or "",
                reasons_json=json.dumps(["Coursework and projects match the core requirements",
                                         "Has used the main tools named in the posting"]),
                gaps_json=json.dumps(["No direct experience with the team's cloud platform"] if score < 80 else []),
                pitch=f"I'm excited about the {title} role at {company} because it builds directly on my recent project work.",
                note="Sample: the form has extra required questions." if status == "needs_manual" else "",
                is_demo=True, found_at=t0 - timedelta(hours=i * 7),
            )
            s.add(job)
            s.commit()
            s.refresh(job)
            if branch:
                if branch not in branch_ids:
                    tr = TailoredResume(resume_id=-1, slot=slot, branch=branch, created_for_job_id=job.id,
                                        edits_json=json.dumps(SAMPLE_EDITS[branch]), is_demo=True,
                                        doc_url="https://docs.google.com/document/")
                    s.add(tr)
                    s.commit()
                    s.refresh(tr)
                    branch_ids[branch] = tr.id
                job.tailored_id = branch_ids[branch]
                s.add(job)
            if status == "applied":
                s.add(Application(job_id=job.id, applied_at=t0 - timedelta(days=i - 2, hours=random.randint(1, 9)),
                                  resume_slot=slot, tailored=bool(branch), score=score,
                                  status=APP_STATUSES[i % len(APP_STATUSES)], is_demo=True,
                                  drive_url="https://docs.google.com/document/" if branch else None))
                if branch:
                    tr = s.get(TailoredResume, branch_ids[branch])
                    tr.uses += 1
                    s.add(tr)
            s.commit()
        for msg, lvl in [("Found 34 postings, 10 new.", "info"),
                         ("Queued Software Engineering Intern at Northwind Labs (match 91, Resume A)", "success"),
                         ("Tailored Resume B for Contoso Health ('E-commerce data analyst'): 2 edits", "info"),
                         ("Reusing the 'E-commerce data analyst' tailored Resume B for Fabrikam", "info"),
                         ("Skipped Marketing Coordinator at Blue Yonder Airlines (match 38)", "info"),
                         ("Coho Winery: The form has extra required questions.", "warn")]:
            s.add(LogEvent(message=f"[sample] {msg}", level=lvl))
        s.commit()


def clear() -> None:
    with session() as s:
        demo_ids = list(s.exec(select(Job.id).where(Job.is_demo == True)))  # noqa: E712
        s.exec(delete(Application).where(Application.is_demo == True))  # noqa: E712
        s.exec(delete(TailoredResume).where(TailoredResume.is_demo == True))  # noqa: E712
        s.exec(delete(Job).where(Job.is_demo == True))  # noqa: E712
        s.exec(delete(LogEvent).where(LogEvent.message.startswith("[sample]")))
        if demo_ids:
            s.exec(delete(LogEvent).where(LogEvent.job_id.in_(demo_ids)))
        s.commit()


def has_demo() -> bool:
    with session() as s:
        return s.exec(select(Job.id).where(Job.is_demo == True)).first() is not None  # noqa: E712
