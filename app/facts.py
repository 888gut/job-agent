"""Career facts: what the tailoring may draw on beyond the resume being tailored.

Sources (all treated as true; nothing else is):
  1. Both base resumes, always the latest uploads (so Resume A can borrow a fact from Resume B).
  2. Profile facts you have confirmed on the Career Facts page. Facts the agent learns from
     conversations are added unconfirmed and are ignored until you confirm them.
"""
from pathlib import Path

from sqlmodel import select

from .db import ProfileFact, Resume, now, session


def confirmed_facts() -> list[ProfileFact]:
    with session() as s:
        return list(s.exec(select(ProfileFact).where(ProfileFact.confirmed == True)  # noqa: E712
                           .order_by(ProfileFact.kind, ProfileFact.id)))


def all_facts() -> list[ProfileFact]:
    with session() as s:
        return list(s.exec(select(ProfileFact).order_by(ProfileFact.confirmed, ProfileFact.kind, ProfileFact.id)))


def transcript() -> dict | None:
    """The uploaded transcript, if any: {filename, path, text, uploaded_at}."""
    from .db import get_settings

    t = get_settings().get("transcript")
    return t if t and Path(t.get("path", "")).exists() else None


def verified_text(base: Resume, others: list[Resume]) -> str:
    """Everything the guardrail treats as true for tailoring `base`."""
    parts = [base.text] + [o.text for o in others]
    for f in confirmed_facts():
        parts.append(f"{f.title} {f.text}")
    if (t := transcript()):
        parts.append(t["text"])
    return "\n".join(parts)


def facts_prompt(others: list[Resume]) -> str:
    lines = []
    for o in others:
        lines.append(f"<other_resume slot=\"{o.slot}\" label=\"{o.label}\">\n{o.text}\n</other_resume>")
    if (t := transcript()):
        lines.append("<transcript note=\"official courses; may be named in the coursework line only\">\n"
                     f"{t['text'][:20000]}\n</transcript>")
    confirmed = confirmed_facts()  # one query, not one per kind
    plain = [f for f in confirmed if f.kind == "fact"]
    if plain:
        lines.append("<confirmed_facts>\n" + "\n".join(f"- {f.text}" for f in plain) + "\n</confirmed_facts>")
    projects = [f for f in confirmed if f.kind == "project"]
    if projects:
        lines.append("<confirmed_projects>\n" + "\n".join(
            f"[{f.id}] {f.title}: {f.text}" for f in projects) + "\n</confirmed_projects>")
    return "\n\n".join(lines)


def add_fact(kind: str, text: str, title: str = "", confirmed: bool = False, source: str = "you") -> ProfileFact:
    with session() as s:
        f = ProfileFact(kind=kind, title=title.strip(), text=text.strip(), confirmed=confirmed, source=source)
        s.add(f)
        s.commit()
        s.refresh(f)
        return f


def update_fact(fact_id: int, **fields) -> ProfileFact | None:
    with session() as s:
        f = s.get(ProfileFact, fact_id)
        if not f:
            return None
        for k, v in fields.items():
            if k in ("kind", "title", "text", "confirmed") and v is not None:
                setattr(f, k, v.strip() if isinstance(v, str) else v)
        f.updated_at = now()
        s.add(f)
        s.commit()
        s.refresh(f)
        return f


def delete_fact(fact_id: int) -> None:
    with session() as s:
        f = s.get(ProfileFact, fact_id)
        if f:
            s.delete(f)
            s.commit()
