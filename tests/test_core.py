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


def _no_claude(**kw):
    raise RuntimeError("no real Claude calls in tests")


@pytest.fixture(autouse=True)
def _never_call_claude(monkeypatch):
    """Any test that reaches the real API client fails fast instead (tests stub what they need)."""
    monkeypatch.setattr(llm, "_client", SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=_no_claude))))

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


# ---------------- the Tailor sub-agent (fake Claude: scripted tool calls) ----------------
def _tool_turn(*calls):
    """One fake API response whose content is the given (name, input) tool calls."""
    blocks = [SimpleNamespace(type="thinking", thinking="")]
    blocks += [SimpleNamespace(type="tool_use", id=f"tu{n}", name=name, input=inp) for n, (name, inp) in enumerate(calls)]
    return SimpleNamespace(stop_reason="tool_use", content=blocks, model="claude-opus-5-5",
                           usage=SimpleNamespace(input_tokens=100, output_tokens=50,
                                                 cache_read_input_tokens=0, cache_creation_input_tokens=0))


class _ScriptedMessages:
    def __init__(self, turns):
        self.turns, self.calls = list(turns), []

    def create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})   # snapshot: the loop appends
        return self.turns.pop(0) if self.turns else _tool_turn(("check_edit", {"paragraph": 6, "new_text": "x"}))


def _scripted(monkeypatch, turns):
    msgs = _ScriptedMessages(turns)
    monkeypatch.setattr(llm, "_client", SimpleNamespace(beta=SimpleNamespace(messages=msgs)))
    return msgs


GOOD_6 = "Trained 6 new hires on checkout procedures and store policy."
BAD_12 = "Built a YouTube channel with 2,000 subscribers."


def test_tailor_subagent_checks_fixes_and_submits_only_passing_edits(monkeypatch, full_pdf):
    from app.db import ClaudeCall, init_db, session
    from sqlmodel import select

    init_db()
    monkeypatch.setattr(tailor, "ask_json", lambda *a, **k: pytest.fail("fell back to the single call"))
    msgs = _scripted(monkeypatch, [
        _tool_turn(("check_edit", {"paragraph": 6, "new_text": GOOD_6}),
                   ("check_edit", {"paragraph": 12, "new_text": BAD_12}),
                   ("check_edit", {"paragraph": 3, "new_text": "Senior Sales Associate"})),
        _tool_turn(("submit_plan", {"edits": [{"paragraph": 6, "new_text": GOOD_6, "why": "a"},
                                              {"paragraph": 12, "new_text": BAD_12, "why": "b"}],
                                    "add_project_id": 0, "add_project_why": ""})),
        _tool_turn(("submit_plan", {"edits": [{"paragraph": 6, "new_text": GOOD_6, "why": "a"}],
                                    "add_project_id": 0, "add_project_why": ""})),
    ])
    with session() as s:
        before = len(s.exec(select(ClaudeCall).where(ClaudeCall.purpose == "Tailor")).all())
    accepted, rejected, added = tailor.plan_tailoring(JOB, str(full_pdf), "claude-opus-5-5")
    assert [e["paragraph"] for e in accepted] == [6] and rejected == [] and added is None
    assert len(msgs.calls) == 3

    first = msgs.calls[0]
    assert first["model"] == "claude-opus-5-5" and first["output_config"] == {"effort": "high"}
    assert first["fallbacks"] == "default" and first["cache_control"] == {"type": "ephemeral"}
    assert {t["name"] for t in first["tools"]} == {"check_edit", "submit_plan"} and all(t["strict"] for t in first["tools"])
    prefix = first["messages"][0]["content"][0]
    assert prefix["cache_control"] == {"type": "ephemeral"} and "RESUME BEING TAILORED" in prefix["text"]

    results = {r["tool_use_id"]: r for r in msgs.calls[1]["messages"][-1]["content"]}
    assert results["tu0"]["content"] == "ok" and not results["tu0"]["is_error"]
    assert "2,000" in results["tu1"]["content"]
    assert "locked" in results["tu2"]["content"]
    refused = msgs.calls[2]["messages"][-1]["content"][0]
    assert refused["is_error"] and "Not submitted" in refused["content"] and "[12]" in refused["content"]
    # the assistant turns go back unchanged (thinking blocks included)
    assert msgs.calls[1]["messages"][1]["content"][0].type == "thinking"
    with session() as s:
        after = len(s.exec(select(ClaudeCall).where(ClaudeCall.purpose == "Tailor")).all())
    assert after - before == 3                                   # one usage row per API request


def test_tailor_subagent_cannot_bypass_the_final_checks(monkeypatch, full_pdf):
    monkeypatch.setattr(tailor, "AGENT_TURNS", 1)                # last turn: the submit is accepted as-is
    monkeypatch.setattr(tailor, "ask_json", lambda *a, **k: pytest.fail("fell back to the single call"))
    _scripted(monkeypatch, [_tool_turn(("submit_plan", {
        "edits": [{"paragraph": 6, "new_text": GOOD_6, "why": "a"}, {"paragraph": 12, "new_text": BAD_12, "why": "b"},
                  {"paragraph": 8, "new_text": "Technical: SQL, Python", "why": "c"}],
        "add_project_id": 0, "add_project_why": ""}))])
    accepted, rejected, _ = tailor.plan_tailoring(JOB, str(full_pdf), "claude-opus-5-5")
    assert [e["paragraph"] for e in accepted] == [6]
    reasons = {e["paragraph"]: " ".join(e["problems"]) for e in rejected}
    assert "2,000" in reasons[12] and "locked" in reasons[8]


def test_tailor_subagent_falls_back_to_one_call(monkeypatch, full_pdf):
    plan = tailor.TailorPlan(edits=[tailor.Edit(paragraph=6, new_text=GOOD_6, why="ok")],
                             add_project_id=0, add_project_why="")
    monkeypatch.setattr(tailor, "ask_json", lambda *a, **k: plan)
    msgs = _scripted(monkeypatch, [])                            # never submits -> hits the turn cap
    accepted, _, _ = tailor.plan_tailoring(JOB, str(full_pdf), "claude-opus-5-5")
    assert len(msgs.calls) == tailor.AGENT_TURNS and [e["paragraph"] for e in accepted] == [6]
    assert "turn left" in msgs.calls[-1]["messages"][-1]["content"][-1]["text"]

    refusal = SimpleNamespace(stop_reason="refusal", content=[], model="m", usage=None)
    msgs = _scripted(monkeypatch, [refusal])                     # refusal -> straight to the fallback
    accepted, _, _ = tailor.plan_tailoring(JOB, str(full_pdf), "claude-opus-5-5")
    assert len(msgs.calls) == 1 and [e["paragraph"] for e in accepted] == [6]


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


def _apply_requests(text, reqs, start=1):
    """Apply delete/insert requests to plain text the way the Docs API would."""
    for r in reqs:
        if "deleteContentRange" in r:
            rg = r["deleteContentRange"]["range"]
            text = text[: rg["startIndex"] - start] + text[rg["endIndex"] - start:]
        elif "insertText" in r:
            i = r["insertText"]["location"]["index"] - start
            text = text[:i] + r["insertText"]["text"] + text[i:]
    return text


@pytest.mark.parametrize("old,new", [
    ("Helped shoppers choose bikes based on how they ride.",
     "Helped shoppers choose bikes, matching frames and gears to how they ride."),
    ("Analyzed regional sales data to inform restocking.",
     "Analyzed regional sales data, tracking weekly sell-through to inform restocking."),
    ("Built dashboards in Excel for the team.", "Built Excel dashboards for the team."),
    ("Trained 6 new hires on checkout procedures.", "Trained 6 new hires on checkout and returns procedures."),
])
def test_edit_requests_produce_exactly_the_new_text(old, new):
    doc = _doc([(old + "\n", PLAIN)])
    reqs, missing = google_store.edit_requests(doc, [{"old": old, "new": new}])
    assert missing == []
    assert _apply_requests(old + "\n", reqs) == new + "\n"   # punctuation such as commas is kept


def test_hyphen_at_line_end_rejoins_without_space(tmp_path):
    doc = pymupdf.open()
    p = doc.new_page(width=612, height=792)
    p.insert_text((49, 104), "SKILLS", fontname="tibo", fontsize=11)
    p.insert_text((65, 130), "•", fontname="helv", fontsize=10)
    p.insert_text((75, 130), "Reviewed weekly sales across all store locations to drive data-", fontname="tiro", fontsize=10)
    p.insert_text((75, 142), "informed buying and restocking decisions.", fontname="tiro", fontsize=10)
    path = tmp_path / "h.pdf"
    doc.save(str(path))
    lay = layout.parse(path)
    bullet = next(b for b in lay.blocks if b.kind == "bullet")
    assert bullet.lines == 2
    assert "drive data-informed buying" in bullet.text   # no stray space after the line-end hyphen


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
        best_slot="Z", branch="supply chain OPERATIONS ", pitch="p", hard_blocks=[], red_flags=[])
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
    assert row[:3] == [app.applied_at.astimezone().strftime("%Y-%m-%d %H:%M"), "Contoso", "Analyst"]
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


def test_dry_run_keeps_job_in_queue_but_real_problems_need_you(monkeypatch, tmp_path):
    from app import handshake, pipeline
    from app.db import Job, init_db, session

    init_db()
    with session() as s:
        jobs = [Job(handshake_id=f"dry{i}", url="u", title="Analyst", company=f"Dry{i}", status="queued") for i in range(2)]
        s.add_all(jobs)
        s.commit()
        ids = [j.id for j in jobs]
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF")
    monkeypatch.setattr(pipeline, "resume_pdf_for", lambda job: pdf)
    monkeypatch.setattr(pipeline, "upload_name_for", lambda job: "Resume.pdf")
    monkeypatch.setattr(pipeline, "applied_today", lambda: 0)
    results = iter([handshake.ApplyResult("ready", "Dry run passed."), handshake.ApplyResult("needs_manual", "Extra questions.")])
    monkeypatch.setattr(pipeline, "_hs", lambda *a, **k: next(results))
    assert pipeline.approve(ids[0])["ok"] is True
    pipeline.approve(ids[1])
    with session() as s:
        assert s.get(Job, ids[0]).status == "queued"            # still has Approve & Submit
        assert s.get(Job, ids[1]).status == "needs_manual"


def test_batch_stops_on_captcha_or_timeout(monkeypatch, tmp_path):
    from concurrent.futures import TimeoutError as FutureTimeout

    from app import handshake, pipeline
    from app.db import Job, init_db, session

    init_db()
    with session() as s:
        jobs = [Job(handshake_id=f"blk{i}", url="u", title="Analyst", company=f"Blk{i}", status="queued") for i in range(4)]
        s.add_all(jobs)
        s.commit()
        ids = [j.id for j in jobs]
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF")
    monkeypatch.setattr(pipeline, "resume_pdf_for", lambda job: pdf)
    monkeypatch.setattr(pipeline, "upload_name_for", lambda job: "Resume.pdf")
    monkeypatch.setattr(pipeline, "applied_today", lambda: 0)
    monkeypatch.setattr(pipeline, "_sleep_unless_stopped", lambda s: None)

    calls = []
    def fake_hs(*a, **k):
        calls.append(1)
        return handshake.ApplyResult("blocked", "Handshake is showing a CAPTCHA.")
    monkeypatch.setattr(pipeline, "_hs", fake_hs)
    pipeline.apply_batch(ids[:2])
    assert len(calls) == 1                                    # stopped after the first CAPTCHA
    with session() as s:
        assert s.get(Job, ids[0]).status == "needs_manual" and s.get(Job, ids[1]).status == "queued"

    def slow_hs(*a, **k):
        raise FutureTimeout()
    monkeypatch.setattr(pipeline, "_hs", slow_hs)
    res = pipeline.approve(ids[2])
    assert res["blocked"] and "check this job on Handshake" in res["message"]
    with session() as s:
        assert s.get(Job, ids[2]).status == "needs_manual"    # never "failed": it may have gone through


def test_daily_cap_counts_from_local_midnight():
    from datetime import datetime, timedelta, timezone

    from app import pipeline
    from app.db import Application, init_db, session

    init_db()
    before = pipeline.applied_today()
    local_midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    with session() as s:
        s.add(Application(job_id=0, applied_at=(local_midnight + timedelta(minutes=1)).astimezone(timezone.utc)))
        s.add(Application(job_id=0, applied_at=(local_midnight - timedelta(minutes=1)).astimezone(timezone.utc)))
        s.commit()
    assert pipeline.applied_today() == before + 1             # yesterday evening doesn't count today


def test_required_question_check_catches_checkboxes_radios_and_dropdowns():
    from app.handshake import UNANSWERED_JS
    sync_api = pytest.importorskip("playwright.sync_api")
    html = """<div id=f>
      <input required value="Pat">
      <input type=checkbox required>
      <input type=radio name=auth required><input type=radio name=auth>
      <select required><option value="">Select</option><option>Yes</option></select>
      <div role=combobox aria-required=true>Select an option</div>
      <input type=file required>
    </div>"""
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome", headless=True)
        except Exception:
            pytest.skip("Chrome not available")
        page = browser.new_page()
        page.set_content(html)
        root = page.locator("#f")
        assert root.evaluate(UNANSWERED_JS) == 4              # checkbox, radio group, select, combobox
        page.check("input[type=checkbox]")
        page.check("input[name=auth] >> nth=1")
        page.select_option("select", "Yes")
        page.evaluate("document.querySelector('[role=combobox]').textContent = 'Yes'")
        assert root.evaluate(UNANSWERED_JS) == 0
        browser.close()


def test_employer_form_fill_only_contact_basics_and_resume(tmp_path):
    from app import handshake
    sync_api = pytest.importorskip("playwright.sync_api")
    contact = {"name": "Pat Doe", "first": "Pat", "last": "Doe", "email": "pat@example.com", "phone": "(555) 010-0000"}
    form = """<form>
      <label>First Name <input name="first_name"></label>
      <label for="ln">Last name</label><input id="ln">
      <input type="email" placeholder="Email address">
      <label>Mobile phone <input type="tel"></label>
      <label>Company <input name="company"></label>
      <label>Why do you want this job? <textarea name="why"></textarea></label>
      <label>Resume/CV <input type="file" name="resume"></label>
      <label>Cover letter <input type="file" name="cover"></label>
    </form>"""
    login = """<form><input type="email" placeholder="Email"><input type="password"></form>"""
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome", headless=True)
        except Exception:
            pytest.skip("Chrome not available")
        page = browser.new_page()
        page.set_content(form)
        out = handshake._fill_employer_page(page, pdf, "Pat_Doe_Resume.pdf", contact)
        assert set(out["filled"]) == {"first name", "last name", "email", "phone"} and out["resume"]
        assert page.input_value("[name=company]") == "" and page.input_value("[name=why]") == ""   # never guesses answers
        assert page.eval_on_selector("[name=resume]", "e => e.files[0].name") == "Pat_Doe_Resume.pdf"
        assert page.eval_on_selector("[name=cover]", "e => e.files.length") == 0
        page.set_content(login)
        out = handshake._fill_employer_page(page, pdf, None, contact)
        assert out["account"] and out["filled"] == [] and not out["resume"]                       # sign-in pages are yours
        browser.close()


def test_contact_for_reads_the_resume_header():
    from app import pipeline
    from app.db import Job, Resume

    r = Resume(slot="A", filename="a.pdf", path="a.pdf", text="PAT DOE\npat@example.com | (555) 010-0000 | City, ST\nEducation")
    pipeline_resumes = pipeline.current_resumes
    try:
        pipeline.current_resumes = lambda: {"A": r}
        c = pipeline.contact_for(Job(handshake_id="c1", url="u", title="t", company="c", resume_slot="A"))
    finally:
        pipeline.current_resumes = pipeline_resumes
    assert c == {"name": "Pat Doe", "first": "Pat", "last": "Doe", "email": "pat@example.com", "phone": "(555) 010-0000"}


def test_handshake_docs_are_reused_and_old_unsent_copies_cleaned(monkeypatch, tmp_path):
    from datetime import timedelta

    from app import pipeline
    from app.db import HandshakeDoc, init_db, now, session

    init_db()
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(b"%PDF tailored v1")
    assert pipeline.known_doc(pdf) == ""
    pipeline.note_doc_use("d-new", pdf, "R.pdf", sent=False)
    assert pipeline.known_doc(pdf) == "d-new"                     # same PDF -> reuse, no new upload
    pdf.write_bytes(b"%PDF tailored v2 (you edited the Doc)")
    assert pipeline.known_doc(pdf) == ""                          # changed PDF -> upload a fresh copy
    old = now() - timedelta(days=8)
    with session() as s:
        s.add(HandshakeDoc(doc_id="d-old", last_used_at=old))
        s.add(HandshakeDoc(doc_id="d-old-sent", last_used_at=old, sent=True))
        s.commit()
    deleted = []
    monkeypatch.setattr(pipeline, "_hs", lambda fn, *a, **k: deleted.append(a[0]) or True)
    monkeypatch.setattr(pipeline, "get_settings", lambda: {"hs_docs_adopted": True})
    r = pipeline.cleanup_handshake_docs()
    assert deleted == ["d-old"] and r["deleted"] == 1           # recent and sent copies are kept
    with session() as s:
        assert s.get(HandshakeDoc, "d-old").deleted and not s.get(HandshakeDoc, "d-old-sent").deleted


def test_handshake_optimizer_replaces_unsent_branch_docs_and_versions_sent_ones(monkeypatch, tmp_path, full_pdf):
    import pymupdf

    from app import google_store, pipeline
    from app.db import Application, Job, Resume, TailoredResume, init_db, session

    init_db()
    with session() as s:
        r = Resume(slot="A", filename="a.pdf", path=str(full_pdf), text="resume a")
        s.add(r)
        s.commit()
        s.refresh(r)
        unsent = TailoredResume(resume_id=r.id, slot="A", branch="Logistics", doc_id="doc-u", doc_url="u/doc-u")
        sent = TailoredResume(resume_id=r.id, slot="A", branch="Finance", doc_id="doc-s", doc_url="u/doc-s", uses=1)
        s.add(unsent)
        s.add(sent)
        s.commit()
        s.refresh(unsent)
        s.refresh(sent)
        s.add(Job(handshake_id="j", url="", title="t", company="c", status="applied", tailored_id=sent.id))
        s.add(Application(job_id=1, resume_slot="A", tailored=True, drive_url="u/doc-s"))
        s.commit()

    draft = tmp_path / "draft.pdf"
    d = pymupdf.open()
    page = d.new_page()
    for i in range(12):
        page.insert_text((72, 72 + 14 * i), "Improved resume text, line %d" % i)
    d.save(str(draft))
    uploaded, trashed, built, ran = [], [], [], []

    def fake_build(tr_id):
        built.append(tr_id)
        with session() as s:
            row = s.get(TailoredResume, tr_id)
            row.pdf_path = str(full_pdf)
            s.add(row)
            s.commit()
            return row

    monkeypatch.setattr(pipeline, "build_doc", fake_build)
    monkeypatch.setattr(pipeline, "_hs", lambda fn, *a, **k: ran.append(a) or {"docx": tmp_path / "d.docx", "pdf": draft, "doc_id": "hs-9"})
    monkeypatch.setattr(google_store, "is_connected", lambda acct: True)
    monkeypatch.setattr(google_store, "upload_as_doc", lambda path, name: uploaded.append(name) or f"new-{len(uploaded)}")
    monkeypatch.setattr(google_store, "doc_url", lambda i: f"u/{i}")
    monkeypatch.setattr(google_store, "trash_file", lambda i: trashed.append(i))

    res = pipeline.optimize_branches()
    assert res["ok"] and len(res["done"]) == 2 and len(ran) == 2
    with session() as s:
        u = s.get(TailoredResume, unsent.id)
        old = s.get(TailoredResume, sent.id)
        new = s.get(TailoredResume, old.superseded_by)
    assert u.doc_id.startswith("new-") and u.hs_optimized_at and "Improved resume" in u.full_text
    assert trashed == ["doc-u"]                                   # unsent: old Doc replaced (to Drive's trash)
    assert old.doc_id == "doc-s" and new.branch == "Finance"      # sent: kept as the record, new version made
    assert new.doc_id.startswith("new-") and new.superseded_by is None
    assert {t.id for t in pipeline.branch_heads()} == {u.id, new.id}
    assert pipeline.find_branch_resume(r.id, "Finance").id == new.id
    assert all(c["id"] != old.id for c in pipeline.tailored_candidates())
    assert pipeline.optimize_branches()["done"] == []             # weekly: not again within 7 days


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


def test_screen_skips_excluded_titles_part_time_and_consulting():
    s = {"exclude_titles": ["intern", "front desk", "senior", "manager"], "skip_part_time": True}
    job = lambda title, kind="Full-time": {"title": title, "job_type": kind}  # noqa: E731
    assert matcher.screen(job("Supply Chain Analyst"), s) is None
    assert matcher.screen(job("Internal Operations Analyst"), s) is None      # whole words only
    assert "intern" in matcher.screen(job("Data Analyst Intern"), s)
    assert "front desk" in matcher.screen(job("Front Desk & Operations Coordinator"), s)
    assert matcher.screen(job("Senior Logistics Analyst"), s)
    assert matcher.screen(job("Business Analyst", "Part-time"), s) == "Part-time role"
    assert matcher.screen(job("Strategy Consultant"), s) == "Consulting role"
    assert matcher.screen(job("Business Analyst", "Part-time"), {**s, "skip_part_time": False}) is None


def test_hard_blocks_and_red_flags_cap_the_score(monkeypatch):
    seen = {}
    fake = matcher.MatchResult(fits=[matcher.ResumeFit(slot="A", score=80, reasons=[], gaps=["Excel"])],
                               best_slot="A", branch="b", pitch="p",
                               hard_blocks=["Requires a current senior"], red_flags=["Commission-only pay"])
    monkeypatch.setattr(matcher, "ask_json", lambda *a, **k: seen.update(system=a[1]) or fake)
    res = matcher.score_job({"title": "Analyst", "company": "c", "location": "", "job_type": "", "description": "d"},
                            {"A": "resume a"}, "claude-opus-5-5", home="New York, NY", commute=75)
    fit = matcher.best_fit(res)
    assert fit.score == matcher.CAP
    assert fit.gaps[:2] == ["Hard requirement: Requires a current senior", "Red flag: Commission-only pay"]
    assert "75 minutes by transit from New York, NY" in seen["system"]


def test_score_and_tailor_send_stable_text_as_cached_prefix(monkeypatch, full_pdf):
    seen = {}
    fake = matcher.MatchResult(fits=[matcher.ResumeFit(slot="A", score=70, reasons=[], gaps=[])],
                               best_slot="A", branch="b", pitch="p", hard_blocks=[], red_flags=[])
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

    def fake_score(job, resumes, model, branches, **_kw):
        scored_with[job["title"]] = {k: list(v) for k, v in branches.items()}
        if job["title"] == "p2":
            raise _api_error(anthropic.OverloadedError, 529)
        score = {"p0": 70, "p1": 72, "p3": 30}[job["title"]]
        return matcher.MatchResult(fits=[matcher.ResumeFit(slot="A", score=score, reasons=[], gaps=[])],
                                   best_slot="A", branch="Brand new family", pitch="", hard_blocks=[], red_flags=[])

    monkeypatch.setattr(pipeline, "_hs", fake_hs)
    monkeypatch.setattr(handshake, "read_posting", fake_read)
    monkeypatch.setattr(pipeline, "extract_job", fake_extract)
    monkeypatch.setattr(pipeline, "score_job", fake_score)
    monkeypatch.setattr(pipeline, "tailor_job", lambda job_id, **_k: tailored.append(job_id))
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


# ---------------- Gmail sync ----------------
def _mail(mid, day, labels=("INBOX",)):
    return {"id": mid, "thread": f"t{mid}", "labels": list(labels), "from": "", "to": "",
            "subject": "", "snippet": "", "date": datetime(2026, 11, day, tzinfo=timezone.utc)}


def test_gmail_apply_verdicts_orders_and_files():
    from app.db import Application
    V = gmail_watch.EmailVerdict
    a1 = Application(id=1, job_id=1, status="Submitted")
    a2 = Application(id=2, job_id=2, status="Submitted")
    a3 = Application(id=3, job_id=3, status="Submitted")
    msgs = [_mail("c1", 17), _mail("i1", 18), _mail("r1", 20), _mail("s3", 17, ("SENT",)), _mail("x", 19)]
    verdicts = [
        V(message_id="r1", application_id=1, category="rejection", note="Closed"),   # newer, listed first
        V(message_id="c1", application_id=1, category="confirmation", note="Received"),
        V(message_id="i1", application_id=2, category="interview", note="Pick a time"),
        V(message_id="s3", application_id=3, category="sent_application", note="Sent resume"),
        V(message_id="x", application_id=0, category="other", note="Job alert"),
    ]
    res = gmail_watch.apply_verdicts([a1, a2, a3], msgs, verdicts, "me@example.com")
    assert a1.status == "Rejected" and a1.email_verified          # newest outcome wins over confirmation
    assert a2.status == "Interview" and not a2.email_verified
    assert a3.status == "Submitted" and a3.email_verified         # sent mail verifies, doesn't "confirm"
    assert res["submitted"] == ["c1", "s3"] and res["interviews"] == ["i1"]
    assert a2.email_link.endswith("#all/ti1") and "authuser=me@example.com" in a2.email_link
    assert set(res["changed"]) == {1, 2, 3}


def test_gmail_confirmation_never_downgrades_interview():
    from app.db import Application
    a = Application(id=1, job_id=1, status="Interview")
    gmail_watch.apply_verdicts([a], [_mail("c", 20)],
                               [gmail_watch.EmailVerdict(message_id="c", application_id=1, category="confirmation", note="")])
    assert a.status == "Interview" and a.email_verified


def test_gmail_query():
    q = gmail_watch.build_query(['Zippo', 'B "Square"', "", "Zippo"], datetime(2026, 11, 16))
    assert q == 'after:2026/11/16 (from:joinhandshake.com OR "B Square" OR "Zippo") -in:chats'


# ---------------- resume picker: originals vs. stored tailored copies ----------------
def test_score_job_scores_tailored_copies_and_picks_the_highest(monkeypatch):
    seen = {}
    fake = matcher.MatchResult(
        fits=[matcher.ResumeFit(slot="A", score=78, reasons=[], gaps=[]),
              matcher.ResumeFit(slot="T5", score=88, reasons=["copy"], gaps=[]),
              matcher.ResumeFit(slot="T99", score=99, reasons=[], gaps=[])],   # not offered: dropped
        best_slot="A", branch="ops analyst", pitch="p", hard_blocks=[], red_flags=[])
    monkeypatch.setattr(matcher, "ask_json", lambda *a, **k: seen.update(k=k, system=a[1]) or fake)
    copies = [{"key": "T5", "id": 5, "slot": "B", "branch": "Ops analyst",
               "edits": [{"old": "Built dashboards", "new": "Built KPI dashboards"}], "added": None,
               "record": {"sent": 3, "interviews": 2}}]
    res = matcher.score_job({"title": "t", "company": "c", "location": "", "job_type": "", "description": "d"},
                            {"A": "resume a", "B": "resume b"}, "claude-opus-5-5", {"B": ["Ops analyst"]},
                            tailored=copies, records={"A": {"sent": 4, "interviews": 0}})
    prefix = seen["k"]["cached_prefix"]
    assert 'key="T5" base="B"' in prefix and 'sent="3" interviews="2"' in prefix
    assert '<resume slot="A" sent="4" interviews="0">' in prefix and "Built KPI dashboards" in prefix
    assert "TRACK RECORD" in seen["system"]
    assert [f.slot for f in res.fits] == ["A", "T5"]
    assert res.best_slot == "T5" and matcher.best_fit(res).score == 88
    assert res.branch == "Ops analyst"                        # matched to the copy's base slot branches

    tie = matcher.MatchResult(fits=[matcher.ResumeFit(slot="T5", score=80, reasons=[], gaps=[]),
                                    matcher.ResumeFit(slot="A", score=80, reasons=[], gaps=[])],
                              best_slot="Z", branch="b", pitch="p", hard_blocks=[], red_flags=[])
    monkeypatch.setattr(matcher, "ask_json", lambda *a, **k: tie)
    res = matcher.score_job({"title": "t", "company": "c", "location": "", "job_type": "", "description": "d"},
                            {"A": "resume a"}, "claude-opus-5-5", tailored=copies)
    assert res.best_slot == "A"                               # a tie goes to the original


def test_scorer_picks_stored_copy_and_tailors_fresh_only_as_last_resort(monkeypatch, resume_pdf):
    from app import pipeline
    from app.db import Application, Job, Resume, TailoredResume, init_db, session

    init_db()
    with session() as s:
        r = Resume(slot="A", filename="pick.pdf", path=str(resume_pdf), pdf_path=str(resume_pdf), text="x")
        s.add(r)
        s.commit()
        good = TailoredResume(resume_id=r.id, slot="A", branch="Picker branch",
                              edits_json=json.dumps([{"paragraph": 6, "old": "a", "new": "b", "why": "w"}]))
        plain = TailoredResume(resume_id=r.id, slot="A", branch="Other branch", uses=9)
        s.add_all([good, plain])
        s.commit()
        sent = Job(handshake_id="pick-sent", url="u", title="Old", company="Old Co", status="applied",
                   resume_slot="A", tailored_id=good.id)
        s.add(sent)
        s.commit()
        s.add(Application(job_id=sent.id, resume_slot="A", tailored=True, status="Interview"))
        s.commit()
        good_id, plain_id = good.id, plain.id

    by_slot, by_copy = pipeline.resume_records()
    assert by_copy[good_id] == {"sent": 1, "interviews": 1}
    cands = pipeline.tailored_candidates()
    assert [c["id"] for c in cands][:2] == [good_id, plain_id]   # interview record first, then most used

    offered = {}

    def fake_score(job, resumes, model, branches, scores=None, **kw):
        offered["keys"] = [c["key"] for c in kw["tailored"]]
        fits = [matcher.ResumeFit(slot=k, score=v, reasons=[], gaps=[]) for k, v in fake_score.scores.items()]
        best = max(fits, key=lambda f: f.score).slot
        return matcher.MatchResult(fits=fits, best_slot=best, branch="Picker branch", pitch="",
                                   hard_blocks=[], red_flags=[])

    monkeypatch.setattr(pipeline, "score_job", fake_score)
    settings = {"model": "m", "min_score": 60, "tailor_below": 80, "exclude_titles": [], "skip_part_time": False}

    # 1) a stored copy scores highest and above the cutoff: it's used, nothing new is made
    fake_score.scores = {"A": 75, f"T{good_id}": 86}
    job = Job(handshake_id="pick-1", url="u", title="Analyst", company="Co")
    considered = pipeline._score_into(job, {"A": "x"}, settings, {})
    assert f"T{good_id}" in offered["keys"] and good_id in considered
    assert (job.tailored_id, job.resume_slot, job.score, job.status) == (good_id, "A", 86, "queued")
    assert not pipeline._needs_fresh(job, settings)

    # 2) the original wins: no tailored copy is linked
    fake_score.scores = {"A": 84, f"T{good_id}": 70}
    pipeline._score_into(job, {"A": "x"}, settings, {})
    assert (job.tailored_id, job.resume_slot) == (None, "A")

    # 3) everything scores under 80: last resort, a fresh copy (not the branch copy already compared)
    fake_score.scores = {"A": 72, f"T{good_id}": 76}
    considered = pipeline._score_into(job, {"A": "x"}, settings, {})
    with session() as s:
        s.add(job)
        s.commit()
        s.refresh(job)
    assert pipeline._needs_fresh(job, settings)
    plans = []
    monkeypatch.setattr(pipeline, "plan_tailoring",
                        lambda *a, **k: plans.append(1) or ([{"paragraph": 6, "old": "a", "new": "c", "why": "w"}], [], None))
    monkeypatch.setattr(pipeline, "build_doc", lambda tr_id: None)
    tr = pipeline.tailor_job(job.id, considered=considered)
    assert plans == [1] and tr.id not in (good_id, plain_id)
    with session() as s:
        job = s.get(Job, job.id)
    assert job.tailored_id == tr.id
    assert not pipeline._needs_fresh(job, settings)         # it has its own copy now: no re-tailoring


def test_scorer_may_favor_a_proven_resume_only_on_a_near_tie(monkeypatch):
    copies = [{"key": "T5", "id": 5, "slot": "A", "branch": "b", "edits": [], "added": None,
               "record": {"sent": 4, "interviews": 3}}]

    def run(a, t, pick):
        fake = matcher.MatchResult(fits=[matcher.ResumeFit(slot="A", score=a, reasons=[], gaps=[]),
                                         matcher.ResumeFit(slot="T5", score=t, reasons=[], gaps=[])],
                                   best_slot=pick, branch="b", pitch="p", hard_blocks=[], red_flags=[])
        monkeypatch.setattr(matcher, "ask_json", lambda *a_, **k: fake)
        return matcher.score_job({"title": "t", "company": "c", "location": "", "job_type": "", "description": "d"},
                                 {"A": "resume a"}, "m", tailored=copies).best_slot

    assert run(84, 81, "T5") == "T5"     # within 5 points: the proven copy may win
    assert run(90, 81, "T5") == "A"      # a poor fit isn't lifted by its record


# ---------------- cover letters ----------------
CL_RESUME = """Jordan Lee
jordan@example.com | (555) 010-2000
Sales Associate, Target, 2022-2024
Built dashboards in Excel to track weekly sales for the store team.
Trained 6 new hires on checkout procedures.
Independent Card Trader, 2022-2026
Bought and sold collectibles independently, tracking prices in a spreadsheet.
Skills: Excel (pivot tables), SQL, Financial modeling (familiarity)"""
CL_JOB = {"title": "Operations Analyst", "company": "Acme Logistics",
          "description": "Analyze shipments with Power BI and QuickBooks. Advanced Excel required. Estimating a plus."}


def _cl_check(sentence):
    from app import cover_letter
    kept, rejected = cover_letter.check_letter([sentence], CL_RESUME, CL_RESUME, CL_JOB)
    return rejected[0]["problems"] if rejected else []


def test_cover_letter_check_allows_true_sentences():
    for s in ["I built dashboards in Excel to track weekly sales for the store team at Target.",
              "At Target, I trained 6 new hires on checkout procedures.",
              "I'm excited about the Operations Analyst role at Acme Logistics.",
              "Through independent trading of collectibles, I learned to track prices closely.",
              "I have used SQL and Excel pivot tables.",
              "I'm eager to learn cost estimating on the job.",
              "Earlier, at Target, I trained 6 new hires on checkout procedures."]:
        assert _cl_check(s) == [], s


def test_cover_letter_check_blocks_fabrication_and_overstatement():
    assert any("Walmart" in p for p in _cl_check("I also worked at Walmart on inventory."))
    assert any("'12'" in p for p in _cl_check("I trained 12 new hires on checkout procedures."))
    assert any("Tableau" in p for p in _cl_check("Tableau dashboards were my main tool."))     # new tool, even first word
    assert any("professional" in p for p in _cl_check("As a professional trader, I managed risk daily."))
    assert any("personal or independent" in p for p in _cl_check("My trading taught me to manage risk."))
    assert any("professional" in p for p in _cl_check("My independent trading was a self-employed business."))
    assert any("Power BI" in p for p in _cl_check("I have built reports in Power BI."))
    assert any("QuickBooks" in p for p in _cl_check("I have experience with QuickBooks."))
    assert any("estimating" in p for p in _cl_check("My cost estimating work prepared me well."))
    assert any("overstates Excel" in p for p in _cl_check("I have advanced Excel skills from my time at Target."))
    assert any("overstates modeling" in p for p in _cl_check("I am experienced in financial modeling."))


def test_cover_letter_drops_rejected_sentences_after_one_retry(monkeypatch):
    from app import cover_letter
    good = ["I'm applying for the Operations Analyst role at Acme Logistics. " + "At Target, I built dashboards in Excel to track weekly sales for the store team. " * 4,
            "I also trained 6 new hires on checkout procedures, and I'm eager to learn cost estimating. " * 4]
    first = cover_letter.LetterDraft(paragraphs=[good[0] + " I have advanced Power BI skills.", good[1] + " I worked at Walmart."])
    second = cover_letter.LetterDraft(paragraphs=[good[0], good[1] + " As a professional trader, I managed risk."])
    calls = []

    def fake(model_cls, system, user, **kw):
        calls.append(user)
        return first if len(calls) == 1 else second

    monkeypatch.setattr(cover_letter, "ask_json", fake)
    paras, rejected = cover_letter.write_letter(CL_JOB, CL_RESUME, "", "", "m")
    assert len(calls) == 2 and "Walmart" in calls[1] and "advanced Power BI" in calls[1]   # the retry is told what failed
    text = " ".join(paras)
    assert "Walmart" not in text and "advanced Power BI" not in text and "professional" not in text
    assert {r["text"] for r in rejected} >= {"I worked at Walmart.", "As a professional trader, I managed risk."}
    monkeypatch.setattr(cover_letter, "ask_json", lambda *a, **k: cover_letter.LetterDraft(paragraphs=["I worked at Walmart."]))
    with pytest.raises(RuntimeError):
        cover_letter.write_letter(CL_JOB, CL_RESUME, "", "", "m")                   # nothing true left: no letter


def _text_pdf(path, text):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(50, 50, 560, 780), text, fontsize=10)
    doc.save(str(path))
    return path


def test_cover_letter_is_a_google_doc_reused_per_job_with_your_edits(monkeypatch, tmp_path):
    from app import cover_letter, pipeline
    from app.db import CoverLetter, Job, init_db, session

    init_db()
    with session() as s:
        job = Job(handshake_id="cl-reuse", url="u", title="Operations Analyst", company="Acme Logistics", status="queued")
        s.add(job)
        s.commit()
        s.refresh(job)
    resume = _text_pdf(tmp_path / "resume.pdf", CL_RESUME)
    para = "At Target, I built dashboards in Excel to track weekly sales for the store team. " * 5
    writes, uploads, exports, doc_text = [], [], [], {"t": ""}
    monkeypatch.setattr(cover_letter, "write_letter",
                        lambda *a: writes.append(a) or ([para, para], [{"text": "x", "problems": ["p"]}]))
    monkeypatch.setattr(cover_letter, "_sources", lambda job: ("", ""))
    monkeypatch.setattr(pipeline, "contact_for", lambda job: {"name": "Jordan Lee", "email": "j@example.com", "phone": ""})
    monkeypatch.setattr(google_store, "upload_as_doc", lambda path, name: uploads.append((path, name)) or "cl-doc")
    monkeypatch.setattr(google_store, "doc_url", lambda i: f"https://docs/{i}")

    def fake_export(doc_id, out):
        exports.append(doc_id)
        return _text_pdf(out, doc_text["t"] or "Jordan Lee\nDear Acme Logistics Hiring Team,\n" + para)

    monkeypatch.setattr(google_store, "export_pdf", fake_export)
    row = cover_letter.ensure(job.id, resume)
    assert len(writes) == 1 and len(uploads) == 1 and uploads[0][1] == "Cover letter - Acme Logistics, Operations Analyst"
    assert row.doc_url == "https://docs/cl-doc" and Path(row.pdf_path).exists() and row.resume_key == "A"
    assert Path(uploads[0][0]).suffix == ".docx"
    doc_text["t"] = "Jordan Lee\nI edited this line myself."        # you edit the Doc in Google Docs
    again = cover_letter.ensure(job.id, resume)
    assert again.id == row.id and len(writes) == 1 and len(uploads) == 1   # reused, not rewritten
    assert len(exports) == 2 and "edited this line myself" in again.text   # fresh export picks up your edit
    out = cover_letter.out(job.id)
    assert out["doc_url"] and out["rejected"] and out["has_pdf"]
    trashed = []
    monkeypatch.setattr(google_store, "trash_file", lambda i: trashed.append(i))
    monkeypatch.setattr(pipeline, "resume_pdf_for", lambda job: resume)        # the resume this job is sent with
    cover_letter.rewrite(job.id)
    assert trashed == ["cl-doc"] and len(writes) == 2
    with session() as s:
        from sqlmodel import select
        assert len(s.exec(select(CoverLetter).where(CoverLetter.job_id == job.id)).all()) == 1


def test_cover_letter_upload_name():
    from app import cover_letter
    assert cover_letter.upload_name("Pat_Doe_Resume.pdf") == "Pat_Doe_CoverLetter.pdf"
    assert cover_letter.upload_name("Resume.pdf") == "CoverLetter.pdf"


def test_cover_letter_written_only_when_the_form_asks(monkeypatch, tmp_path):
    from app import cover_letter, handshake
    from app.db import CoverLetter, HandshakeDoc, Job, init_db, session

    init_db()
    with session() as s:
        job = Job(handshake_id="cl-attach", url="u", title="Analyst", company="Acme", status="queued")
        s.add(job)
        s.commit()
        s.refresh(job)
    pdf = _text_pdf(tmp_path / "cover.pdf", "Letter text")
    made = []

    def fake_ensure(job_id, resume_pdf=None):
        made.append(job_id)
        with session() as s:
            row = CoverLetter(job_id=job_id, doc_id="d", doc_url="u", pdf_path=str(pdf), text="Letter text")
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    monkeypatch.setattr(cover_letter, "ensure", fake_ensure)
    monkeypatch.setattr(cover_letter, "export", lambda letter_id: cover_letter.get(job.id))
    on = {"cover_letters": True, "cover_letters_always": False}

    # the form doesn't ask: no letter is written
    seen = []
    res, cover = cover_letter.apply_with_letter(job, pdf, "R_Resume.pdf",
                                                lambda c: seen.append(c) or handshake.ApplyResult("ready", "ok"), on)
    assert res.status == "ready" and seen == [None] and made == [] and cover is None

    # the form asks: written once, then the application is tried again with it
    seen.clear()
    outcomes = iter([handshake.ApplyResult("needs_cover", "asks"), handshake.ApplyResult("ready", "ok", "r1", "c1")])
    res, cover = cover_letter.apply_with_letter(job, pdf, "R_Resume.pdf", lambda c: seen.append(c) or next(outcomes), on)
    assert res.status == "ready" and made == [job.id] and seen[0] is None
    assert seen[1]["name"] == "R_CoverLetter.pdf" and seen[1]["text"] == "Letter text" and seen[1]["doc_id"] == ""
    cover_letter.note_use(job, res, cover)
    with session() as s:
        assert s.get(HandshakeDoc, "c1") and not s.get(HandshakeDoc, "c1").sent    # covered by the weekly cleanup

    # next time the same letter is offered up front, and picked from Handshake instead of re-uploaded
    seen.clear()
    res, cover = cover_letter.apply_with_letter(job, pdf, "R_Resume.pdf",
                                                lambda c: seen.append(c) or handshake.ApplyResult("applied", "ok"), on)
    assert seen[0]["doc_id"] == "c1" and made == [job.id]

    # cover letters turned off: the job goes to you instead of being sent without one
    res, cover = cover_letter.apply_with_letter(job, pdf, "R_Resume.pdf",
                                                lambda c: handshake.ApplyResult("needs_cover", "asks"),
                                                {"cover_letters": False})
    assert res.status == "needs_manual" and "cover letter" in res.note


# A copy of Handshake's application box (structure as seen on a real posting, ids shortened).
HS_BOX = """<div role="dialog"><form>
  <fieldset><h4>Attach your resume</h4><div role="status"><h5>R_Resume.pdf</h5><a href="/docs/111">Preview document</a>
    <button class="rosetta-inline-alert__close-button" aria-label="Close">x</button></div></fieldset>
  <fieldset><h4>Attach your cover letter</h4><div id="cl">
    <input role="combobox" type="search" placeholder="Search your cover letters">
    <div role="listbox" hidden><div role="option" id="x-select-0">Old_CoverLetter.pdf</div><div role="option" id="x-select-1">R_CoverLetter.pdf</div></div>
    <select hidden><option value="500">500</option><option value="777">777</option></select>
    <label for="up">Upload new</label><input id="up" name="file-Cover Letter" type="file"></div></fieldset>
  <button type="button">Submit Application</button></form></div>
<script>
  const done = (name, id) => { document.getElementById('cl').innerHTML = '<div role="status"><h5>' + name +
    '</h5><a href="/docs/' + id + '">Preview document</a><button class="rosetta-inline-alert__close-button">x</button></div>'; };
  document.getElementById('up').addEventListener('change', e => done(e.target.files[0].name, '900'));
  document.querySelector('[role=combobox]').addEventListener('click', () => document.querySelector('[role=listbox]').hidden = false);
  document.getElementById('x-select-1').addEventListener('click', () => done('R_CoverLetter.pdf', '777'));
</script>"""


def test_handshake_box_cover_letter_is_found_and_attached(tmp_path):
    from app import handshake
    sync_api = pytest.importorskip("playwright.sync_api")
    pdf = tmp_path / "cover_9.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome", headless=True)
        except Exception:
            pytest.skip("Chrome not available")
        page = browser.new_page()
        page.set_content(HS_BOX)
        dlg = page.locator("[role=dialog]")
        box = handshake._cover_box(dlg)
        assert box is not None and handshake._attached_doc_id(dlg) == "111"   # the resume link comes first
        assert handshake._attach_cover_letter(box, {"pdf": pdf, "name": "R_CoverLetter.pdf", "doc_id": ""}) is None
        assert handshake._attached_doc_id(box) == "900" and "R_CoverLetter.pdf" in box.inner_text()   # uploaded
        page = browser.new_page()                                               # fresh page: the script declares consts
        page.set_content(HS_BOX)
        box = handshake._cover_box(page.locator("[role=dialog]"))
        assert handshake._attach_cover_letter(box, {"pdf": pdf, "name": "R_CoverLetter.pdf", "doc_id": "777"}) is None
        assert handshake._attached_doc_id(box) == "777"                        # picked, not uploaded again
        page.set_content(HS_BOX.replace("Attach your cover letter", "Attach your transcript").replace("file-Cover Letter", "file-Transcript"))
        assert handshake._cover_box(page.locator("[role=dialog]")) is None    # no cover letter asked for
        browser.close()


def test_employer_page_cover_letter_upload_or_text_box(tmp_path):
    from app import handshake
    sync_api = pytest.importorskip("playwright.sync_api")
    contact = {"name": "Pat Doe", "first": "Pat", "last": "Doe", "email": "pat@example.com", "phone": ""}
    form = """<form><label>Resume/CV <input type="file" name="resume"></label>
      <label>Cover letter <input type="file" name="cover"></label></form>"""
    text_form = """<form><label>Resume <input type="file" name="resume"></label>
      <label for="cl">Cover Letter</label><textarea id="cl"></textarea>
      <label>Anything else? <textarea name="other"></textarea></label></form>"""
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    cover = {"pdf": pdf, "name": "Pat_Doe_CoverLetter.pdf", "doc_id": "", "text": "Dear team,\nA true letter."}
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome", headless=True)
        except Exception:
            pytest.skip("Chrome not available")
        page = browser.new_page()
        page.set_content(form)
        out = handshake._fill_employer_page(page, pdf, "Pat_Doe_Resume.pdf", contact)
        assert out["cover_wanted"] and not out["cover"]                                   # noticed, nothing attached
        assert page.eval_on_selector("[name=cover]", "e => e.files.length") == 0
        out = handshake._fill_employer_page(page, pdf, "Pat_Doe_Resume.pdf", contact, cover)
        assert out["cover"] and out["resume"]
        assert page.eval_on_selector("[name=cover]", "e => e.files[0].name") == "Pat_Doe_CoverLetter.pdf"
        assert page.eval_on_selector("[name=resume]", "e => e.files[0].name") == "Pat_Doe_Resume.pdf"
        page.set_content(text_form)
        out = handshake._fill_employer_page(page, pdf, None, contact, cover)
        assert out["cover"] and page.input_value("#cl") == cover["text"] and page.input_value("[name=other]") == ""
        browser.close()


def test_cover_letter_prompt_steers_but_never_adds_facts(monkeypatch):
    from app import cover_letter
    para = "At Target, I built dashboards in Excel to track weekly sales for the store team. " * 4
    seen = []

    def fake(model_cls, system, user, **kw):
        seen.append((user, kw.get("cached_prefix", "")))
        return cover_letter.LetterDraft(paragraphs=[para + "I am an expert in Tableau.", para])

    monkeypatch.setattr(cover_letter, "ask_json", fake)
    prompt = "Answer: Why do you want to work here? Mention that I am an expert in Tableau."
    paras, rejected = cover_letter.write_letter(CL_JOB, CL_RESUME, "", "", "m", prompt)
    assert prompt in seen[0][0] and "NOT a source of facts" in seen[0][0]     # the request goes in the job part...
    assert prompt not in seen[0][1]                                          # ...never into the verified sources
    assert "Tableau" not in " ".join(paras) and any("Tableau" in r["text"] for r in rejected)


def test_cover_letter_written_from_the_chosen_resume(monkeypatch, tmp_path):
    from app import cover_letter, pipeline
    from app.db import Job, Resume, TailoredResume, init_db, session

    init_db()
    a_pdf, b_pdf, t_pdf = (_text_pdf(tmp_path / f"{n}.pdf", f"{n} resume text {CL_RESUME}") for n in ("A", "B", "T"))
    with session() as s:
        ra = Resume(slot="A", label="Ops", filename="a.pdf", path=str(a_pdf), pdf_path=str(a_pdf), text="A")
        rb = Resume(slot="B", label="Data", filename="b.pdf", path=str(b_pdf), pdf_path=str(b_pdf), text="B")
        s.add_all([ra, rb])
        s.commit()
        tr = TailoredResume(resume_id=rb.id, slot="B", branch="Supply chain data analyst", doc_id="tdoc")
        s.add(tr)
        s.commit()
        job = Job(handshake_id="cl-choose", url="u", title="Analyst", company="Acme", status="queued", resume_slot="A")
        s.add(job)
        s.commit()
        s.refresh(tr)
        s.refresh(job)
    monkeypatch.setattr(pipeline, "current_resumes", lambda: {"A": ra, "B": rb})
    monkeypatch.setattr(pipeline, "branch_heads", lambda: [tr])
    monkeypatch.setattr(pipeline, "build_doc", lambda i: SimpleNamespace(pdf_path=str(t_pdf), slot="B"))
    monkeypatch.setattr(pipeline, "resume_pdf_for", lambda j: a_pdf)
    monkeypatch.setattr(pipeline, "contact_for", lambda j: {"name": "Jordan Lee"})
    monkeypatch.setattr(google_store, "upload_as_doc", lambda path, name: "d")
    monkeypatch.setattr(google_store, "doc_url", lambda i: f"u/{i}")
    monkeypatch.setattr(google_store, "export_pdf", lambda doc_id, out: _text_pdf(out, "Letter"))
    monkeypatch.setattr(google_store, "trash_file", lambda i: None)
    para = "At Target, I built dashboards in Excel to track weekly sales for the store team. " * 4
    used = []
    monkeypatch.setattr(cover_letter, "write_letter",
                        lambda jd, resume_text, verified, facts_text, model, prompt="": used.append((resume_text[:1], prompt)) or ([para, para], []))
    monkeypatch.setattr(cover_letter, "_sources", lambda slot: used.append(("slot", slot)) or ("", ""))

    opts = cover_letter.resume_options()
    assert [o["key"] for o in opts] == ["A", "B", f"T{tr.id}"]
    assert opts[2]["label"] == "Resume B · Supply chain data analyst (tailored)"

    row = cover_letter.create(job.id)                                       # default: the agent's pick for the job
    assert row.resume_key == "A" and used[-1] == ("A", "") and ("slot", "A") in used
    row = cover_letter.rewrite(job.id, "Lead with sales.", f"T{tr.id}")   # a tailored branch resume instead
    assert row.resume_key == f"T{tr.id}" and used[-1] == ("T", "Lead with sales.") and ("slot", "B") in used
    assert row.prompt == "Lead with sales." and cover_letter.get(job.id).id == row.id

    with pytest.raises(ValueError):
        cover_letter.create_standalone("Acme", "Analyst", "too short", "B")
    solo = cover_letter.create_standalone("Globex", "Planner", "Plan inventory and shipments. " * 5, "B")
    assert solo.job_id == 0 and solo.company == "Globex" and used[-1][0] == "B"
    listed = cover_letter.list_all()
    assert {(c["company"], c["resume_label"]) for c in listed} >= {
        ("Globex", "Resume B (original) · Data"), ("Acme", "Resume B · Supply chain data analyst (tailored)")}
    assert cover_letter.get(0) is None                                      # pasted postings never attach to a job
    cover_letter.delete(solo.id)
    assert all(c["id"] != solo.id for c in cover_letter.list_all())
