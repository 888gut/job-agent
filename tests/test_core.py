import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

_TMP = Path(tempfile.mkdtemp())
os.environ["JOB_AGENT_DATA"] = str(_TMP / "data")  # never touch the real data folder
os.environ["JOB_AGENT_DB"] = f"sqlite:///{_TMP.as_posix()}/test.db"

import pymupdf  # noqa: E402
import pytest  # noqa: E402
from docx import Document  # noqa: E402

from app import gmail_watch, google_store, layout, llm, matcher, tailor  # noqa: E402

ORIGINAL = """Jordan Lee
Experience
Sales Associate, Target, 2022-2024
Built dashboards in Excel to track weekly sales for the store team.
Trained 6 new hires on checkout procedures.
Skills: Python, Excel, SQL, Tableau, public speaking"""


# ---------------- a resume PDF in the same style as the real ones ----------------
def make_resume_pdf(path: Path, full: bool = False) -> Path:
    doc = pymupdf.open()
    p = doc.new_page(width=612, height=792)
    L, B, BT, R = 49, 59, 75, 563

    def t(x, y, s, font="tiro", size=10):
        p.insert_text((x, y), s, fontname=font, fontsize=size)

    t(245, 60, "Jordan Lee", "tibo", 16)
    t(220, 82, "jordan@example.com | (555) 010-2000 | LinkedIn", "tiro", 9)
    t(L, 104, "EXPERIENCE", "tibo", 11)
    p.draw_line((L, 110), (R, 110), width=0.75)
    t(B, 128, "Sales Associate", "tibo")
    t(470, 128, "June 2022 - 2024", "tiro")
    t(B, 146, "Target", "tiit")
    t(65, 164, "•", "helv")
    t(BT, 164, "Built dashboards in Excel to track weekly sales for the store team and shared them with")
    t(BT, 176, "managers every Monday morning.")
    t(65, 194, "•", "helv")
    t(BT, 194, "Trained 6 new hires on checkout procedures.")
    t(L, 216, "SKILLS", "tibo", 11)
    p.draw_line((L, 222), (R, 222), width=0.75)
    t(B, 240, "Technical:", "tibo")
    t(104, 240, " Python, Excel, SQL, Tableau, public speaking")
    if full:
        t(L, 262, "PROJECTS", "tibo", 11)
        p.draw_line((L, 268), (R, 268), width=0.75)
        t(B, 286, "Content Creation", "tibo")
        t(B, 304, "Creator", "tiit")
        t(65, 322, "•", "helv")
        t(BT, 322, "Built a YouTube channel with 1,000 subscribers.")
        t(L, 344, "EDUCATION", "tibo", 11)
        p.draw_line((L, 350), (R, 350), width=0.75)
        t(B, 368, "State College", "tibo")
        t(B, 386, "Relevant Coursework: Statistics, Accounting")
    doc.save(str(path))
    return path


@pytest.fixture
def resume_pdf(tmp_path):
    return make_resume_pdf(tmp_path / "resume.pdf")


@pytest.fixture
def full_pdf(tmp_path):
    return make_resume_pdf(tmp_path / "full.pdf", full=True)


# ---------------- layout ----------------
def test_layout_parse_kinds_and_wrapping(resume_pdf):
    lay = layout.parse(resume_pdf)
    kinds = [b.kind for b in lay.blocks]
    assert kinds == ["name", "contact", "heading", "role", "sub", "bullet", "bullet", "heading", "labeled"]
    role, bullet, labeled = lay.blocks[3], lay.blocks[5], lay.blocks[8]
    assert (role.left, role.right) == ("Sales Associate", "June 2022 - 2024")
    assert bullet.lines == 2 and bullet.text.endswith("managers every Monday morning.")  # wrapped line rejoined
    assert labeled.left == "Technical:" and labeled.text.startswith("Technical: Python")
    assert [b.editable for b in lay.blocks] == [False] * 5 + [True, True, False, True]
    assert round(lay.margin_left) == 49 and round(lay.margin_right) == 49
    assert round(lay.bullet_text_x) == 75


def test_fit_check_same_number_of_lines(resume_pdf):
    lay = layout.parse(resume_pdf)
    one_line = lay.blocks[6]
    assert layout.fits(lay, one_line, "Trained 6 new hires on checkout and returns procedures.")[0]
    too_long = "Trained 6 new hires on checkout procedures, " * 4
    ok, old_n, new_n = layout.fits(lay, one_line, too_long)
    assert not ok and old_n == 1 and new_n > 1


def test_build_docx_mirrors_layout(resume_pdf, tmp_path):
    lay = layout.parse(resume_pdf)
    out = layout.build_docx(lay, tmp_path / "master.docx")
    d = Document(str(out))
    texts = [p.text for p in d.paragraphs]
    assert texts[0] == "Jordan Lee"
    assert texts[3] == "Sales Associate\tJune 2022 - 2024"        # right-aligned date via tab
    assert texts[5].startswith("•\t") and lay.blocks[5].text in texts[5]
    assert d.paragraphs[0].runs[0].bold and d.paragraphs[0].runs[0].font.size.pt == 16
    assert d.paragraphs[2].runs[0].font.small_caps                    # section heading style
    assert d.paragraphs[4].runs[0].italic                             # company line
    assert d.paragraphs[8].runs[0].bold and d.paragraphs[8].runs[0].text == "Technical:"
    assert {r.font.name for p in d.paragraphs for r in p.runs} <= {"Times New Roman", "Arial"}
    links = [r for r in d.part.rels.values() if "hyperlink" in r.reltype]
    assert links == [] or all(r.is_external for r in links)


def test_calibrate_moves_spacing_toward_original(resume_pdf):
    import copy

    target = layout.parse(resume_pdf)
    rendered = copy.deepcopy(target)
    for b in rendered.blocks[3:]:      # rendered copy runs 5pt low from block 3 on, and starts 2pt high
        b.top += 5
    for b in rendered.blocks:
        b.top -= 2
    before = [b.space_before for b in target.blocks]
    m0 = target.margin_top
    assert layout.calibrate(target, rendered)
    assert target.margin_top == round(m0 + 2, 1)
    assert target.blocks[3].space_before == max(0.0, round(before[3] - 5, 1))
    assert [b.space_before for b in target.blocks[4:]] == before[4:]
    assert not layout.calibrate(target, target)  # already aligned -> nothing to do


def test_fit_check_uses_text_right_edge(resume_pdf):
    lay = layout.parse(resume_pdf)
    # the text box ends at the longest line, inside the heading rule; dates have their own edge
    assert lay.content_right < lay.page_w - lay.margin_right
    assert lay.date_right <= lay.content_right
    assert lay.width_for("bullet") == lay.content_right - lay.bullet_text_x
    for b in lay.blocks:  # every original paragraph still fits in its original number of lines
        if b.editable:
            assert layout.fits(lay, b, b.text) == (True, b.lines, b.lines)


# ---------------- guardrail ----------------
def test_guardrail_allows_rewording():
    old = "Built dashboards in Excel to track weekly sales for the store team."
    new = "Built weekly sales dashboards in Excel, giving the store team clear reporting."
    assert tailor.guardrail(ORIGINAL, old, new) == []


def test_guardrail_allows_reordering_skills():
    old = "Skills: Python, Excel, SQL, Tableau, public speaking"
    new = "Skills: SQL, Tableau, Python, Excel, public speaking"
    assert tailor.guardrail(ORIGINAL, old, new) == []


def test_guardrail_blocks_new_skill():
    old = "Skills: Python, Excel, SQL, Tableau, public speaking"
    new = "Skills: Python, Excel, SQL, Tableau, Kubernetes, public speaking"
    assert any("Kubernetes" in p for p in tailor.guardrail(ORIGINAL, old, new))


def test_guardrail_blocks_new_number_and_employer():
    old = "Trained 6 new hires on checkout procedures."
    assert any("'12'" in p for p in tailor.guardrail(ORIGINAL, old, "Trained 12 new hires on checkout procedures."))
    assert any("Amazon" in p for p in tailor.guardrail(ORIGINAL, old, "Trained 6 new hires at Amazon on checkout procedures."))


def test_guardrail_blocks_new_tech_token():
    old = "Built dashboards in Excel to track weekly sales for the store team."
    new = "Built dashboards in Excel and C++ to track weekly sales for the store team."
    assert tailor.guardrail(ORIGINAL, old, new)


# ---------------- tailoring plan on a PDF (fake Claude) ----------------
JOB = {"title": "t", "company": "c", "description": "d"}


def test_only_bullets_and_coursework_are_editable(full_pdf):
    lay = layout.parse(full_pdf)
    kinds = [b.kind for b in lay.blocks]
    assert kinds[9:] == ["heading", "role", "sub", "bullet", "heading", "role", "text"]
    assert tailor.editable_indexes(lay) == {5, 6, 12, 15}  # 2 experience bullets, project bullet, coursework


def test_plan_tailoring_rules(monkeypatch, full_pdf):
    plan = tailor.TailorPlan(edits=[
        tailor.Edit(paragraph=6, new_text="Trained 6 new hires on checkout procedures and store policy.", why="ok"),
        tailor.Edit(paragraph=3, new_text="Senior Sales Associate", why="title"),
        tailor.Edit(paragraph=8, new_text="Technical: SQL, Python, Excel, Tableau, public speaking", why="skills"),
        tailor.Edit(paragraph=5, new_text="Built Excel dashboards tracking weekly sales " * 6, why="too long"),
        tailor.Edit(paragraph=15, new_text="Relevant Coursework: Statistics, Supply Chain Management", why="transcript"),
        tailor.Edit(paragraph=12, new_text="Built a YouTube channel with 2,000 subscribers.", why="bad number"),
    ], add_project_id=0, add_project_why="")
    monkeypatch.setattr(tailor, "ask_json", lambda *a, **k: plan)
    verified = "Transcript: SUPPLY CHAIN MANAGEMENT  A-  3 credits"
    accepted, rejected, added = tailor.plan_tailoring(JOB, str(full_pdf), "claude-opus-5-5", verified=verified)
    assert sorted(e["paragraph"] for e in accepted) == [6, 15]   # transcript course allowed in coursework
    reasons = {e["paragraph"]: " ".join(e["problems"]) for e in rejected}
    assert "locked" in reasons[3] and "locked" in reasons[8]     # titles and skills stay as they are
    assert "lines" in reasons[5]
    assert "2,000" in reasons[12]
    assert added is None


def _project(confirmed=True, title="AI Job-Application Agent",
             text="Built a Python agent that finds and tailors job applications."):
    from app import facts
    from app.db import init_db

    init_db()
    return facts.add_fact("project", text, title, confirmed=confirmed)


def test_add_project_only_confirmed_and_only_with_room(monkeypatch, full_pdf, resume_pdf):
    good, pending = _project(), _project(confirmed=False)

    def run(pdf, fact_id):
        plan = tailor.TailorPlan(edits=[], add_project_id=fact_id, add_project_why="relevant")
        monkeypatch.setattr(tailor, "ask_json", lambda *a, **k: plan)
        return tailor.plan_tailoring(JOB, str(pdf), "claude-opus-5-5")

    _, _, added = run(full_pdf, good.id)
    assert added == {"fact_id": good.id, "title": good.title, "text": good.text, "why": "relevant"}
    _, _, added = run(full_pdf, pending.id)                      # unconfirmed facts are never used
    assert added is None
    _, rejected, added = run(resume_pdf, good.id)                # no Projects section -> refused
    assert added is None and "no Projects section" in rejected[0]["problems"][0]
    long = _project(text="Built a Python agent that finds, scores, ranks and tailors job applications " * 3)
    _, rejected, added = run(full_pdf, long.id)
    assert added is None and "longer than one line" in rejected[0]["problems"][0]


def test_project_room_respects_page_bottom(full_pdf):
    lay = layout.parse(full_pdf)
    assert layout.project_room(lay, "Title", "One short bullet.")[0]
    for b in lay.blocks:          # pretend the page is already full
        b.top += 330
    ok, reason = layout.project_room(lay, "Title", "One short bullet.")
    assert not ok and "room" in reason


def test_verified_text_uses_other_resume_confirmed_facts_and_transcript(tmp_path):
    from app import facts
    from app.db import Resume, init_db, save_settings

    init_db()
    facts.add_fact("fact", "Handled about 300 customers per shift at FedEx Office.", confirmed=True)
    facts.add_fact("fact", "Unconfirmed claim about Kubernetes.", confirmed=False)
    tpath = tmp_path / "transcript.pdf"
    tpath.write_bytes(b"x")
    save_settings({"transcript": {"filename": "t.pdf", "path": str(tpath), "text": "OPERATIONS MANAGEMENT B+",
                                  "uploaded_at": "2026-11-17"}})
    a, b = Resume(slot="A", filename="a", path="a", text="Resume A text"), Resume(slot="B", filename="b", path="b", text="Resume B text")
    v = facts.verified_text(a, [b])
    assert "Resume B text" in v and "300 customers" in v and "OPERATIONS MANAGEMENT" in v
    assert "Kubernetes" not in v
    p = facts.facts_prompt([b])
    assert "<other_resume" in p and "<transcript" in p and "Kubernetes" not in p
    save_settings({"transcript": None})
    assert facts.transcript() is None


# ---------------- Google Docs edit requests ----------------
def _doc(*paras):
    """Fake Docs API document: each para is a list of (text, style) runs."""
    content, idx = [], 1
    for runs in paras:
        elements = []
        for text, style in runs:
            elements.append({"startIndex": idx, "endIndex": idx + len(text),
                             "textRun": {"content": text, "textStyle": style}})
            idx += len(text)
        content.append({"startIndex": elements[0]["startIndex"], "paragraph": {"elements": elements}})
    return {"body": {"content": content}}


BOLD = {"bold": True, "fontSize": {"magnitude": 10, "unit": "PT"}}
PLAIN = {"fontSize": {"magnitude": 10, "unit": "PT"}}


def test_edit_requests_keep_label_style_and_only_replace_changed_part():
    doc = _doc([("Technical:", BOLD), (" Python, Excel, SQL\n", PLAIN)],
               [("•", PLAIN), ("\tBuilt dashboards in Excel for the team.\n", PLAIN)])
    edits = [{"old": "Technical: Python, Excel, SQL", "new": "Technical: SQL, Python, Excel"},
             {"old": "Built dashboards in Excel for the team.", "new": "Built Excel dashboards for the team."}]
    reqs, missing = google_store.edit_requests(doc, edits)
    assert missing == []
    deletes = [r["deleteContentRange"]["range"] for r in reqs if "deleteContentRange" in r]
    inserts = [r["insertText"] for r in reqs if "insertText" in r]
    styles = [r["updateTextStyle"] for r in reqs if "updateTextStyle" in r]
    # applied from the end of the document backwards so indexes stay valid
    assert deletes[0]["startIndex"] > deletes[1]["startIndex"]
    # the bold label "Technical:" (indexes 1-11) is never deleted
    assert all(d["startIndex"] >= 12 for d in deletes)
    assert {i["text"] for i in inserts} == {"SQL, Python, Excel", "Excel dashboards"}
    assert all(s["textStyle"].get("bold") is None for s in styles)  # inserted text keeps the plain style


def test_project_requests_copy_styles_and_insert_after_last_project():
    TITLE = {"bold": True, "fontSize": {"magnitude": 10, "unit": "PT"}}
    ARIAL = {"weightedFontFamily": {"fontFamily": "Arial"}, "fontSize": {"magnitude": 10, "unit": "PT"}}
    doc = _doc([("Content Creation\n", TITLE)],
               [("•", ARIAL), ("\tBuilt a YouTube channel.\n", PLAIN)],
               [("EDUCATION\n", BOLD)])
    for i, el in enumerate(doc["body"]["content"]):
        el["endIndex"] = el["paragraph"]["elements"][-1]["endIndex"]
        el["paragraph"]["paragraphStyle"] = {"indentStart": {"magnitude": 10 + i, "unit": "PT"}, "namedStyleType": "X"}
    reqs = google_store.project_requests(doc, "Built a YouTube channel.", "Content Creation", "Built a YouTube channel.",
                                         "AI Agent", "Built an agent.")
    ins = reqs[0]["insertText"]
    bullet_para = doc["body"]["content"][1]
    assert ins["location"]["index"] == bullet_para["endIndex"] - 1      # right before the bullet's newline
    assert ins["text"] == "\nAI Agent\n•\tBuilt an agent."
    pstyles = [r["updateParagraphStyle"] for r in reqs if "updateParagraphStyle" in r]
    assert pstyles[0]["paragraphStyle"] == {"indentStart": {"magnitude": 10, "unit": "PT"}}   # title like "Content Creation"
    assert pstyles[1]["paragraphStyle"] == {"indentStart": {"magnitude": 11, "unit": "PT"}}   # bullet like the bullet
    tstyles = [r["updateTextStyle"]["textStyle"] for r in reqs if "updateTextStyle" in r]
    assert tstyles[0].get("bold") is True                                   # title bold
    assert tstyles[1]["weightedFontFamily"]["fontFamily"] == "Arial"        # bullet glyph like the original
    assert tstyles[2].get("bold") is None                                   # bullet text regular


def test_edit_requests_reports_missing_text():
    doc = _doc([("Hello world\n", PLAIN)])
    _, missing = google_store.edit_requests(doc, [{"old": "Not in doc", "new": "x"}])
    assert missing == ["Not in doc"]


# ---------------- branches: one-off tailored Doc, reused for the same branch ----------------
def test_branch_reuse_and_base_resume_untouched(monkeypatch, resume_pdf):
    from app import pipeline
    from app.db import Job, Resume, TailoredResume, init_db, session

    init_db()
    before = hashlib.sha256(resume_pdf.read_bytes()).hexdigest()
    with session() as s:
        r = Resume(slot="A", filename="resume.pdf", path=str(resume_pdf), pdf_path=str(resume_pdf), text="x")
        s.add(r)
        jobs = [Job(handshake_id=f"b{i}", url="u", title="Analyst", company=f"Co{i}", resume_slot="A",
                    branch="E-commerce data analyst") for i in range(3)]
        jobs[2].branch = "Supply chain operations"
        s.add_all(jobs)
        s.commit()
        ids = [j.id for j in jobs]

    calls = {"plan": 0, "copy": 0}

    def fake_plan(*a, **k):
        calls["plan"] += 1
        return [{"paragraph": 6, "old": "a", "new": "b", "why": "w"}], [], None

    def fake_copy(master, name):
        calls["copy"] += 1
        return f"doc{calls['copy']}"

    monkeypatch.setattr(pipeline, "plan_tailoring", fake_plan)
    monkeypatch.setattr(google_store, "upload_as_doc", lambda path, name: "master1")
    monkeypatch.setattr(google_store, "copy_doc", fake_copy)
    monkeypatch.setattr(google_store, "apply_doc_edits", lambda doc_id, edits: [])
    monkeypatch.setattr(google_store, "export_pdf", lambda doc_id, out: out.write_bytes(resume_pdf.read_bytes()) or out)
    monkeypatch.setattr(google_store, "trash_file", lambda file_id: None)
    from app.config import DATA
    assert str(DATA).startswith(str(_TMP))

    first = pipeline.tailor_job(ids[0])
    second = pipeline.tailor_job(ids[1])          # same branch -> reused, no new Doc, no new Claude call
    third = pipeline.tailor_job(ids[2])           # new branch -> its own one-off Doc
    assert second.id == first.id and third.id != first.id
    assert calls == {"plan": 2, "copy": 2}
    with session() as s:
        assert s.get(TailoredResume, first.id).doc_id == "doc1"
        assert s.get(Job, ids[1]).tailored_id == first.id
    assert pipeline.branches_by_slot()["A"] == ["E-commerce data analyst", "Supply chain operations"]

    fresh = pipeline.tailor_job(ids[1], fresh=True)  # explicit re-tailor makes a new one-off
    assert fresh.id not in (first.id, third.id)

    with session() as s:
        job = s.get(Job, ids[0])
    assert pipeline.resume_pdf_for(job).name == f"tailored_{first.id}.pdf"
    with session() as s:
        job.tailored_id = None
    assert pipeline.resume_pdf_for(job) == resume_pdf                  # original PDF otherwise
    assert hashlib.sha256(resume_pdf.read_bytes()).hexdigest() == before  # base never modified


# ---------------- matcher structure ----------------
def test_score_job_clamps_fixes_slot_and_reuses_branch_name(monkeypatch):
    fake = matcher.MatchResult(
        fits=[matcher.ResumeFit(slot="A", score=130, reasons=["r"], gaps=[]),
              matcher.ResumeFit(slot="B", score=-5, reasons=[], gaps=["g"])],
        best_slot="Z", branch="supply chain OPERATIONS ", pitch="p")
    monkeypatch.setattr(matcher, "ask_json", lambda *a, **k: fake)
    res = matcher.score_job({"title": "t", "company": "c", "location": "", "job_type": "", "description": "d"},
                            {"A": "resume a", "B": "resume b"}, "claude-opus-5-5",
                            {"A": ["Supply chain operations"]})
    assert res.best_slot == "A"
    assert matcher.best_fit(res).score == 100
    assert res.fits[1].score == 0


def test_strict_schema_closes_objects():
    schema = llm._strict(matcher.MatchResult.model_json_schema())
    fit = schema["$defs"]["ResumeFit"]
    assert fit["additionalProperties"] is False
    assert set(fit["required"]) == {"slot", "score", "reasons", "gaps"}
    assert schema["additionalProperties"] is False and "branch" in schema["required"]


# ---------------- sheet row & email classification ----------------
def test_sheet_row_format():
    job = SimpleNamespace(company="Contoso", title="Analyst", location="Remote",
                          url="https://app.joinhandshake.com/jobs/123", handshake_id="123")
    app = SimpleNamespace(applied_at=datetime(2026, 11, 17, 9, 30, tzinfo=timezone.utc),
                          resume_slot="B", tailored=True, score=74, status="Submitted")
    row = google_store.sheet_row(job, app, "https://docs/x", "E-commerce data analyst")
    assert len(row) == len(google_store.SHEET_HEADER)
    assert row[:3] == ["2026-11-17 09:30", "Contoso", "Analyst"]
    assert row[5:10] == ["Resume B", "Yes", "E-commerce data analyst", 74, "Submitted"]
    assert google_store.SHEET_HEADER[ord(google_store.STATUS_COL) - ord("A")] == "Status"


def test_email_classify():
    assert gmail_watch.classify("Your application was submitted to Contoso") == "Confirmed"
    assert gmail_watch.classify("Contoso would like to schedule an interview") == "Interview"
    assert gmail_watch.classify("Unfortunately we are not moving forward") == "Rejected"
    assert gmail_watch.classify("New jobs for you this week") is None


# ---------------- misc ----------------
def test_handshake_id_is_unique():
    from sqlalchemy.exc import IntegrityError

    from app.db import Job, init_db, session

    init_db()
    with session() as s:
        s.add(Job(handshake_id="555", url="u"))
        s.commit()
    with pytest.raises(IntegrityError):
        with session() as s:
            s.add(Job(handshake_id="555", url="u2"))
            s.commit()


def test_job_id_regex():
    from app.handshake import JOB_ID_RX

    assert JOB_ID_RX.search("https://app.joinhandshake.com/jobs/9876543?ref=x").group(1) == "9876543"
    assert JOB_ID_RX.search("https://app.joinhandshake.com/job-search/12345678").group(1) == "12345678"
    assert JOB_ID_RX.search("https://app.joinhandshake.com/employers/1234") is None


def test_location_filter_from_card_text():
    from app.pipeline import location_ok

    ny = "Institutional Investor\nSales Operations Analyst\n$90-100K/yr · Full-time\nNew York City, NY\n2wk ago"
    bk = "Hudson Freight\nOperations Analyst\nBrooklyn, NY"
    la = "Acme\nData Analyst\nLos Angeles, CA\n3d ago"
    remote = "Acme\nData Analyst\nRemote\n3d ago"
    unknown = "Five Oaks Nursery\nAccounts Payable Specialist\n$50-60K/yr · Full-time\nPromoted"
    assert location_ok(ny, "New York, NY", True) and location_ok(bk, "New York, NY", True)
    assert not location_ok(la, "New York, NY", True)
    assert location_ok(remote, "New York, NY", True) and not location_ok(remote, "New York, NY", False)
    assert location_ok(unknown, "New York, NY", True)       # no place on the card: let scoring decide
    assert location_ok(la, "", True)                         # no location set: keep everything


def test_apply_batch_runs_in_order_and_stops_at_daily_cap(monkeypatch):
    from app import pipeline
    from app.db import Job, init_db, session

    init_db()
    with session() as s:
        jobs = [Job(handshake_id=f"batch{i}", url="u", title=f"Job {i}", company=f"Co{i}", status="queued") for i in range(4)]
        s.add_all(jobs)
        s.commit()
        ids = [j.id for j in jobs]
    calls = []

    def fake_approve(jid):
        calls.append(jid)
        if len(calls) == 3:
            return {"ok": False, "message": "Daily cap of 2 applications reached."}
        return {"ok": True, "message": "Applied."}

    monkeypatch.setattr(pipeline, "approve", fake_approve)
    monkeypatch.setattr(pipeline, "_sleep_unless_stopped", lambda s: None)
    pipeline.apply_batch(ids)
    assert calls == ids[:3]                                   # stopped at the daily cap
    assert pipeline.batch["running"] is False and pipeline.batch["done"] == 3
    assert [r["ok"] for r in pipeline.batch["results"]] == [True, True, False]

    calls.clear()
    pipeline.batch["stop"] = False
    monkeypatch.setattr(pipeline, "approve", lambda jid: (calls.append(jid), pipeline.batch.update(stop=True))[0] or {"ok": True, "message": "ok"})
    pipeline.apply_batch(ids[3:] + ids[3:])
    assert len(calls) == 1                                    # Stop takes effect after the current job


def test_dashboard_only_answers_local_requests():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app, base_url="http://127.0.0.1:8765")
    assert c.get("/api/settings").status_code == 200                                   # own page: fine
    assert c.get("/api/settings", headers={"Host": "attacker.example"}).status_code == 403   # DNS rebinding
    assert c.post("/api/apply-batch/stop", headers={"Origin": "https://attacker.example"}).status_code == 403
    assert c.post("/api/apply-batch/stop", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.post("/api/apply-batch/stop", headers={"Origin": "http://127.0.0.1:8765"}).status_code == 200
    r = c.get("/api/settings")
    assert r.headers["x-frame-options"] == "DENY"
    assert c.get("/docs").status_code in (403, 404) and c.get("/openapi.json").status_code == 404


def test_settings_roundtrip():
    from app.db import get_settings, init_db, save_settings

    init_db()
    save_settings({"daily_cap": 7, "queries": ["x"]})
    s = get_settings()
    assert s["daily_cap"] == 7 and s["queries"] == ["x"] and s["dry_run"] is True
    json.dumps(s)


# ---------------- speed / cost / robustness (no real API, browser or Google calls) ----------------
class _FakeMessages:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def create(self, **kw):
        self.calls.append(kw)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=out)])


def _fake_client(monkeypatch, outcomes):
    msgs = _FakeMessages(outcomes)
    monkeypatch.setattr(llm, "_client", SimpleNamespace(beta=SimpleNamespace(messages=msgs)))
    waits = []
    monkeypatch.setattr(llm, "_sleep", waits.append)
    return msgs, waits


def _api_error(cls, status):
    import httpx2

    return cls("busy", response=httpx2.Response(status, request=httpx2.Request("POST", "https://x")), body=None)


EXTRACT_JSON = json.dumps({"title": "t", "company": "c", "location": "", "job_type": "", "deadline": "",
                           "description": "d", "apply_type": "unknown"})


def test_ask_json_cached_prefix_goes_first_and_settings_unchanged(monkeypatch):
    msgs, _ = _fake_client(monkeypatch, [EXTRACT_JSON, EXTRACT_JSON])
    llm.ask_json(matcher.JobExtract, "sys", "the job", cached_prefix="the resumes")
    kw = msgs.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["output_config"]["format"]["type"] == "json_schema"
    first, second = kw["messages"][0]["content"]
    assert first == {"type": "text", "text": "the resumes", "cache_control": {"type": "ephemeral"}}
    assert second == {"type": "text", "text": "the job"}
    llm.ask_json(matcher.JobExtract, "sys", "plain")              # no prefix -> plain string, as before
    assert msgs.calls[1]["messages"][0]["content"] == "plain"


def test_ask_json_waits_out_transient_errors_but_not_bad_requests(monkeypatch):
    import anthropic

    msgs, waits = _fake_client(monkeypatch, [_api_error(anthropic.OverloadedError, 529),
                                             _api_error(anthropic.RateLimitError, 429), EXTRACT_JSON])
    assert llm.ask_json(matcher.JobExtract, "s", "u").title == "t"
    assert len(msgs.calls) == 3 and waits == list(llm.RETRY_WAIT)

    msgs, waits = _fake_client(monkeypatch, [_api_error(anthropic.OverloadedError, 529)] * 3)
    with pytest.raises(anthropic.OverloadedError):
        llm.ask_json(matcher.JobExtract, "s", "u")
    assert len(msgs.calls) == 1 + llm.EXTRA_ATTEMPTS

    msgs, waits = _fake_client(monkeypatch, [_api_error(anthropic.BadRequestError, 400)])
    with pytest.raises(anthropic.BadRequestError):
        llm.ask_json(matcher.JobExtract, "s", "u")
    assert len(msgs.calls) == 1 and waits == []


def test_score_and_tailor_send_stable_text_as_cached_prefix(monkeypatch, full_pdf):
    seen = {}
    fake = matcher.MatchResult(fits=[matcher.ResumeFit(slot="A", score=70, reasons=[], gaps=[])],
                               best_slot="A", branch="b", pitch="p")
    monkeypatch.setattr(matcher, "ask_json", lambda *a, **k: seen.update(score=(a, k)) or fake)
    matcher.score_job({"title": "Analyst", "company": "c", "location": "", "job_type": "", "description": "d"},
                      {"A": "resume a", "B": "resume b"}, "claude-opus-5-5", {"A": ["Ops"]})
    (_, _, user), k = seen["score"]
    assert "resume a" in k["cached_prefix"] and "resume b" in k["cached_prefix"]
    assert "resume a" not in user and "Analyst" in user and "Slot A: Ops" in user   # job + branches after

    plan = tailor.TailorPlan(edits=[], add_project_id=0, add_project_why="")
    monkeypatch.setattr(tailor, "ask_json", lambda *a, **k: seen.update(tailor=(a, k)) or plan)
    tailor.plan_tailoring({"title": "Analyst", "company": "c", "description": "the job"}, str(full_pdf),
                          "claude-opus-5-5", facts_prompt="FACTS")
    (_, _, user), k = seen["tailor"]
    assert "(editable)" in k["cached_prefix"] and "FACTS" in k["cached_prefix"]
    assert "the job" in user and "the job" not in k["cached_prefix"]
    assert k["effort"] == "high"


def test_compact_text_keeps_words_and_lines():
    raw = "Title\r\n\n\n\n   Responsibilities:\t\t Build   reports \n\n \n- SQL  required  "
    assert matcher.compact_text(raw) == "Title\n\nResponsibilities: Build reports\n\n- SQL required"


def test_parse_layout_cache_returns_private_copies_and_sees_new_files(monkeypatch, full_pdf, resume_pdf):
    calls = []
    real = layout.parse
    monkeypatch.setattr(layout, "parse", lambda p: calls.append(p) or real(p))
    tailor._parse_cache.clear()
    a = tailor.parse_layout(full_pdf)
    a.blocks[0].text = "changed"                       # callers may mutate (calibrate does)
    b = tailor.parse_layout(full_pdf)
    assert len(calls) == 1 and b.blocks[0].text == "Jordan Lee"
    full_pdf.write_bytes(resume_pdf.read_bytes())      # a new upload at the same path
    os.utime(full_pdf, ns=(1, 1))
    assert len(tailor.parse_layout(full_pdf).blocks) == len(real(resume_pdf).blocks)
    assert len(calls) == 2


def test_search_reads_in_browser_thread_and_reviews_in_parallel(monkeypatch):
    import threading
    import time

    import anthropic

    from app import handshake, pipeline
    from app.db import Job, init_db, session

    init_db()
    main = threading.get_ident()
    reads, scored_with, tailored = [], {}, []

    def fake_hs(fn, *args, timeout=300):
        assert threading.get_ident() == main             # browser work never leaves the run thread
        return fn(*args)

    def fake_read(url):
        reads.append(url)
        if url.endswith("captcha"):
            raise RuntimeError("Handshake is showing a CAPTCHA.")
        return f"page {url}"

    def fake_extract(page_text, url, model):
        assert threading.get_ident() != main
        time.sleep(0.05 if url.endswith("p0") else 0)   # p0 finishes extraction last
        return matcher.JobExtract(title=url[-2:], company="Co", location="", job_type="", deadline="",
                                  description="d", apply_type="handshake")

    def fake_score(job, resumes, model, branches):
        scored_with[job["title"]] = {k: list(v) for k, v in branches.items()}
        if job["title"] == "p2":
            raise _api_error(anthropic.OverloadedError, 529)
        score = {"p0": 70, "p1": 72, "p3": 30}[job["title"]]
        return matcher.MatchResult(fits=[matcher.ResumeFit(slot="A", score=score, reasons=[], gaps=[])],
                                   best_slot="A", branch="Brand new family", pitch="")

    monkeypatch.setattr(pipeline, "_hs", fake_hs)
    monkeypatch.setattr(handshake, "read_posting", fake_read)
    monkeypatch.setattr(pipeline, "extract_job", fake_extract)
    monkeypatch.setattr(pipeline, "score_job", fake_score)
    monkeypatch.setattr(pipeline, "tailor_job", lambda job_id: tailored.append(job_id))
    monkeypatch.setattr(pipeline, "branches_by_slot", lambda: {"A": ["Existing"]})
    settings = {"model": "claude-opus-5-5", "min_score": 55, "tailor_below": 80}
    new = [(f"par{i}", f"https://h/jobs/p{i}") for i in range(4)] + [("parX", "https://h/captcha"),
                                                                     ("parY", "https://h/never")]
    pipeline._review_postings(new, {"A": "resume"}, settings)

    assert reads == [u for _, u in new[:5]]               # CAPTCHA stops reading further postings
    # scoring keeps posting order: p1 sees the branch p0 is about to be tailored in, p0 doesn't
    assert scored_with["p0"] == {"A": ["Existing"]}
    assert scored_with["p1"] == {"A": ["Brand new family", "Existing"]}
    with session() as s:
        rows = {j.handshake_id: j for j in s.exec(select_jobs()) if j.handshake_id.startswith("par")}
    assert rows["par0"].status == rows["par1"].status == "queued"
    assert rows["par3"].status == "skipped"
    assert "par2" not in rows                              # API failure: no half-made row, retried next run
    assert sorted(tailored) == sorted([rows["par0"].id, rows["par1"].id])
    assert all(j.status != "new" for j in rows.values())


def select_jobs():
    from sqlmodel import select

    from app.db import Job

    return select(Job)


def test_same_branch_tailoring_is_not_duplicated_when_parallel(monkeypatch, resume_pdf):
    import threading
    import time

    from app import pipeline
    from app.db import Job, Resume, init_db, session

    init_db()
    with session() as s:
        r = Resume(slot="A", filename="r.pdf", path=str(resume_pdf), pdf_path=str(resume_pdf), text="x")
        s.add(r)
        jobs = [Job(handshake_id=f"lock{i}", url="u", title="Analyst", company=f"Co{i}", resume_slot="A",
                    branch="Parallel branch") for i in range(3)]
        s.add_all(jobs)
        s.commit()
        ids = [j.id for j in jobs]
    plans = []

    def slow_plan(*a, **k):
        plans.append(1)
        time.sleep(0.1)
        return [{"paragraph": 6, "old": "a", "new": "b", "why": "w"}], [], None

    monkeypatch.setattr(pipeline, "plan_tailoring", slow_plan)
    monkeypatch.setattr(pipeline, "build_doc", lambda tr_id: None)
    threads = [threading.Thread(target=pipeline.tailor_job, args=(i,)) for i in ids]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(plans) == 1                                 # one Claude call, one Doc; the others reuse it
    with session() as s:
        assert len({s.get(Job, i).tailored_id for i in ids}) == 1
