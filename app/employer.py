"""Applying on an employer's own site, all the way to Submit.

You gave the agent standing permission to submit applications on employer sites (Nov 17, 2026).
It fills the form from your resume and confirmed Career Facts, attaches the resume and (when the
form has a place for one) a cover letter, and clicks Submit. It still stops and hands the job to
you ("Needs you") when the site wants an account, a sign-in or a password, shows a CAPTCHA, or asks
a required question your resume and Career Facts can't answer truthfully. It never creates
accounts, types passwords or solves CAPTCHAs. Applications sent this way are marked "Employer
site" on the dashboard.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable

from pydantic import BaseModel

# Every visible, empty form field in one frame: tagged with data-ja-id so answers can be set later.
FIELDS_JS = r"""(start) => {
  const deepAll = (sel, root = document) => { const out = [...root.querySelectorAll(sel)]; for (const n of root.querySelectorAll('*')) if (n.shadowRoot) out.push(...deepAll(sel, n.shadowRoot)); return out; };
  const rootOf = (el) => { const r = el.getRootNode(); return r && r.querySelector ? r : document; };
  const vis = (el) => !!(el.offsetParent || el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden';
  const text = (el) => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const labelOf = (el) => {
    let t = '';
    if (el.id) { const l = rootOf(el).querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) t = text(l); }
    if (!t && el.closest('label')) t = text(el.closest('label'));
    if (!t && el.getAttribute('aria-labelledby')) t = el.getAttribute('aria-labelledby').split(' ').map(i => text(rootOf(el).getElementById ? rootOf(el).getElementById(i) : document.getElementById(i))).join(' ');
    if (!t) t = el.getAttribute('aria-label') || el.placeholder || '';
    if (!t) { const box = el.closest('fieldset,[role=group],.field,.form-group,li,div'); if (box) t = text(box).slice(0, 200); }
    return (t || el.name || el.id || '').slice(0, 300);
  };
  const required = (el) => el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(labelOf(el));
  let n = start;
  const out = [], groups = {};
  for (const el of deepAll('input, textarea, select')) {
    const type = (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'file', 'password', 'search'].includes(type)) continue;
    if (el.disabled || el.readOnly) continue;
    if (type === 'radio' || type === 'checkbox') {
      if (!vis(el) && !(el.labels && [...el.labels].some(vis))) continue;
      const key = type + ':' + (el.name || el.id);
      const opt = (el.labels && el.labels[0] ? text(el.labels[0]) : '') || el.value;
      if (type === 'radio' && el.name) {
        if (!groups[key]) {
          const box = el.closest('fieldset,[role=radiogroup],[role=group]');
          const q = box ? text(box.querySelector('legend,[id$=label],label,span,p') || box).slice(0, 300) : labelOf(el);
          groups[key] = { id: 'f' + (n++), kind: 'radio', label: q, options: [], required: el.required, checked: false };
          out.push(groups[key]);
        }
        const g = groups[key];
        el.setAttribute('data-ja-id', g.id + ':' + g.options.length);
        g.options.push(opt);
        if (el.checked) g.checked = true;
        continue;
      }
      if (el.checked) continue;
      const id = 'f' + (n++);
      el.setAttribute('data-ja-id', id);
      out.push({ id, kind: 'checkbox', label: labelOf(el) || opt, options: [], required: el.required });
      continue;
    }
    if (!vis(el)) continue;
    if (el.tagName === 'SELECT') {
      if (el.value && el.selectedIndex > 0) continue;
      const id = 'f' + (n++);
      el.setAttribute('data-ja-id', id);
      out.push({ id, kind: 'select', label: labelOf(el), options: [...el.options].map(o => o.text.trim()).filter(Boolean).slice(0, 80), required: required(el) });
      continue;
    }
    if (el.value) continue;
    const id = 'f' + (n++);
    el.setAttribute('data-ja-id', id);
    out.push({ id, kind: el.tagName === 'TEXTAREA' ? 'textarea' : type, label: labelOf(el), options: [], required: required(el) });
  }
  return out.filter(f => f.kind !== 'radio' || !f.checked).map(({ checked, ...f }) => f);
}"""

SET_JS = r"""([id, kind, value]) => {
  const deepAll = (sel, root = document) => { const out = [...root.querySelectorAll(sel)]; for (const n of root.querySelectorAll('*')) if (n.shadowRoot) out.push(...deepAll(sel, n.shadowRoot)); return out; };
  const q = (sel) => deepAll(sel)[0];
  const fire = (el) => { el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); el.dispatchEvent(new Event('blur', { bubbles: true })); };
  if (kind === 'radio') {
    const el = q('[data-ja-id="' + id + ':' + value + '"]');
    if (!el) return false; el.click(); fire(el); return true;
  }
  const el = q('[data-ja-id="' + id + '"]');
  if (!el) return false;
  if (kind === 'checkbox') { if (!el.checked) el.click(); fire(el); return true; }
  if (el.tagName === 'SELECT') {
    const o = [...el.options].find(o => o.text.trim() === value) || [...el.options].find(o => o.text.trim().toLowerCase().startsWith(String(value).toLowerCase()));
    if (!o) return false; el.value = o.value; fire(el); return true;
  }
  const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value); fire(el); return true;
}"""

SUBMIT_RX = re.compile(r"^\s*(submit( (my |your )?application)?|send( my)? application|apply( now)?|complete( my)? application|finish)\s*$", re.I)
NEXT_RX = re.compile(r"^\s*(next( step| page)?|continue|save (and|&) continue|review( application)?|proceed|"
                     r"i.?m interested|apply (for|to) this (job|position|role)|start( (my|your))? application|apply manually)\s*[›>→]?\s*$", re.I)
DONE_RX = re.compile(r"thank you for (applying|your application|your interest)|application (has been |was )?(received|submitted|complete)|"
                     r"successfully (submitted|applied)|we('ve| have) received your application|you('ve| have) (successfully )?applied", re.I)
ERROR_RX = re.compile(r"(this field is|is) required|please (fill|complete|enter|select|correct)|can.?t be blank|invalid", re.I)
CAPTCHA_HOSTS = ("recaptcha", "hcaptcha", "turnstile", "challenges.cloudflare", "arkoselabs", "funcaptcha",
                 "captcha-delivery", "datadome", "perimeterx", "px-cloud", "px-captcha", "geetest")
HUMAN_RX = re.compile(r"verify you are (a )?human|are you a robot|unusual traffic|press (and|&) hold", re.I)


class Answer(BaseModel):
    id: str
    value: str          # text to type; for select/radio the exact option text; for checkbox "check" or ""


class Answers(BaseModel):
    answers: list[Answer]
    cannot_answer: list[str]   # labels of REQUIRED fields that can't be answered truthfully from the record


ANSWER_SYSTEM = """You fill in a job application form on an employer's website for the candidate, using
ONLY what their resume, confirmed facts and contact details say. The candidate has authorized the
submission; your job is accuracy.

Rules:
- Never invent experience, employers, dates, degrees, GPAs, salaries, certifications, licenses,
  clearances, references or skills. If a REQUIRED field needs something the record doesn't contain,
  leave it out of `answers` and list its label in `cannot_answer`. Optional fields you can't answer:
  just leave them out.
- Work authorization / sponsorship / age / relocation / start date / salary: answer only if the record
  states it. Otherwise it's `cannot_answer` when required.
- Voluntary self-identification (gender, race, ethnicity, veteran, disability): choose the option
  that declines to answer ("I don't wish to answer", "Decline to self-identify", "Prefer not to say").
  If there is no such option, leave it out.
- "How did you hear about us": choose Handshake (or the closest option: job board, university/college,
  online). Text field: "Handshake".
- Checkboxes: "check" only to acknowledge or certify the application (e.g. "I certify this
  information is accurate", "I have read the privacy notice") or for a true statement from the
  record. Never subscribe to marketing, texts or talent newsletters.
- For select and radio fields, `value` must be one of the listed options, copied exactly.
- Short free-text questions ("Why are you interested in this role?"): 2-4 plain sentences in first
  person, drawn only from the record and the job; no claims beyond it.
- Links (LinkedIn, website, portfolio): only if the record contains the URL.
- Never fill a password, a username for creating an account, or a Social Security number. If one is
  required, list it in `cannot_answer`.
"""


def answer_fields(fields: list[dict], job: dict, record: str) -> Answers:
    from .db import get_settings
    from .llm import ask_json

    lines = [f'- id={f["id"]} kind={f["kind"]}{" REQUIRED" if f["required"] else ""} label="{f["label"]}"'
             + (f' options={f["options"]}' if f["options"] else "") for f in fields]
    user = (f"<job>\n{job.get('title','')} at {job.get('company','')}\n{(job.get('description') or '')[:4000]}\n</job>\n\n"
            "<form_fields>\n" + "\n".join(lines) + "\n</form_fields>")
    return ask_json(Answers, ANSWER_SYSTEM, user, model=get_settings().get("model", "claude-opus-5-5"),
                    effort="low", max_tokens=8000, cached_prefix=record)


@dataclass
class SiteResult:
    status: str                     # applied | ready | needs_manual | unconfirmed
    note: str = ""
    answered: list[str] = field(default_factory=list)


def _captcha(page) -> bool:
    """A CAPTCHA on screen that a person would have to solve. Many forms carry an invisible
    background check ("protected by reCAPTCHA"); that isn't one, and the agent never tries to
    get around a real one."""
    for fr in page.frames:
        if not any(h in fr.url for h in CAPTCHA_HOSTS) or "size=invisible" in fr.url:
            continue
        try:
            el = fr.frame_element()
            box = el.bounding_box()
            if el.is_visible() and box and box["width"] >= 60 and box["height"] >= 60:
                return True
        except Exception:
            continue
    try:
        return bool(HUMAN_RX.search(page.inner_text("body", timeout=3000)[:5000]))
    except Exception:
        return False


def challenge_on_page(page) -> str | None:
    return "The employer's site is showing a CAPTCHA." if _captcha(page) else None


def _password_visible(page) -> bool:
    for fr in page.frames:
        try:
            pw = fr.locator("input[type=password]")
            if any(pw.nth(i).is_visible() for i in range(pw.count())):
                return True
        except Exception:
            continue
    return False


def _button(page, rx):
    for fr in page.frames:
        for role in ("button", "link"):
            try:
                b = fr.get_by_role(role, name=rx)
                for i in range(b.count()):
                    if b.nth(i).is_visible() and b.nth(i).is_enabled():
                        return b.nth(i)
            except Exception:
                continue
        try:
            b = fr.locator("input[type=submit]:visible")
            if b.count() and rx is SUBMIT_RX:
                return b.first
        except Exception:
            continue
    return None


def _click(el) -> None:
    """A real click; if the button sits where a real click can't reach it, the page's own click."""
    try:
        el.click(timeout=8000)
    except Exception:
        el.evaluate("e => e.click()")


def _body(page) -> str:
    """Visible text of the page and its form frames (e.g. a Greenhouse iframe)."""
    parts = []
    for fr in page.frames:
        if fr is not page.main_frame and fr.url in ("", "about:blank"):
            continue
        try:
            parts.append(fr.inner_text("body", timeout=3000)[:8000])
        except Exception:
            continue
    return " ".join(parts)


def finish(page, job: dict, record: str, dry_run: bool, refill: Callable[[], None],
           answer: Callable[[list[dict], dict, str], Answers] = answer_fields, max_steps: int = 8) -> SiteResult:
    """Fill and submit the employer form open in `page` (contact basics, resume and cover letter
    were already filled by the caller; `refill` repeats that on each new step of the form)."""
    answered: list[str] = []
    submitted = False
    for _ in range(max_steps):
        captcha = _captcha(page)
        if _password_visible(page):
            return SiteResult("needs_manual", "The employer's site wants you to sign in or create an account. "
                              "That part is yours; the agent never types passwords or creates accounts.", answered)
        if DONE_RX.search(_body(page)):
            return SiteResult("applied", "Applied on the employer's site.", answered)
        fields, n = [], 0
        for fr in page.frames:
            try:
                got = fr.evaluate(FIELDS_JS, n)
            except Exception:
                continue
            for f in got:
                f["frame"] = fr
            fields += got
            n += len(got) + 1000
        if captcha and not fields:
            return SiteResult("needs_manual", "The employer's site is showing a CAPTCHA. Finish this one yourself.", answered)
        if fields:
            ans = answer([{k: v for k, v in f.items() if k != "frame"} for f in fields], job, record)
            if ans.cannot_answer:
                return SiteResult("needs_manual", "The employer's form asks something your resume and Career Facts don't answer: "
                                  + "; ".join(a[:80] for a in ans.cannot_answer[:4]) + ". Finish it yourself, or add that to Career Facts.", answered)
            by_id = {f["id"]: f for f in fields}
            for a in ans.answers:
                f = by_id.get(a.id)
                if not f or not a.value:
                    continue
                value = a.value
                if f["kind"] == "radio":
                    if a.value not in f["options"]:
                        continue
                    value = str(f["options"].index(a.value))
                elif f["kind"] == "checkbox" and a.value.lower() != "check":
                    continue
                try:
                    if f["frame"].evaluate(SET_JS, [a.id, f["kind"], value]):
                        answered.append(f["label"][:60])
                except Exception:
                    continue
            time.sleep(1)
        if captcha:  # the agent never solves one: everything else is filled in, the rest is yours
            return SiteResult("needs_manual", "Filled in on the employer's site, but it has an \"I'm not a robot\" check. "
                              "Open the Handshake tab in the dashboard, tick it and click Submit.", answered)
        submit, nxt = _button(page, SUBMIT_RX), _button(page, NEXT_RX)
        if submit is None and nxt is None:
            if submitted:
                break
            return SiteResult("needs_manual", "Couldn't find the Submit button on the employer's form. Finish this one yourself.", answered)
        if submit is None:
            before = page.url + str(len(_body(page)))
            _click(nxt)
            time.sleep(3)
            if page.url + str(len(_body(page))) == before and ERROR_RX.search(_body(page)):
                return SiteResult("needs_manual", "The employer's form wouldn't move past a step (a required answer is missing). Finish it yourself.", answered)
            refill()
            continue
        if dry_run:
            return SiteResult("ready", "Dry run: the employer's form is filled in and stopped before Submit.", answered)
        _click(submit)
        submitted = True
        for _ in range(10):
            time.sleep(2)
            if DONE_RX.search(_body(page)):
                return SiteResult("applied", "Applied on the employer's site.", answered)
        if _captcha(page):
            return SiteResult("needs_manual", "The employer's site showed a CAPTCHA after Submit. Finish this one yourself.", answered)
        if _button(page, SUBMIT_RX) is not None and ERROR_RX.search(_body(page)):
            return SiteResult("needs_manual", "The employer's form didn't accept the answers (a required field is still empty). Finish it yourself.", answered)
        if _button(page, NEXT_RX) is not None or _button(page, SUBMIT_RX) is None:
            refill()
            continue  # a further step after "Submit"
        break
    if submitted:
        return SiteResult("unconfirmed", "Clicked Submit on the employer's site but couldn't confirm it went through. Check your email for a confirmation.", answered)
    return SiteResult("needs_manual", "The employer's form had more steps than expected. Finish it yourself.", answered)


STEP2_RX = re.compile(r"step\s*2|continue to|go to (the )?(employer|company)|apply on (the )?(employer|company)", re.I)


def submit_on_site(*args) -> dict:
    """A tab closing mid-way must never make the browser wrapper start over (it could apply twice)."""
    try:
        return _submit_on_site(*args)
    except Exception as e:
        if "closed" in str(e).lower():
            return {"kind": "form", "status": "unconfirmed", "doc_id": "", "email": "", "cover_used": False,
                    "note": "The employer's page closed mid-way, so the agent can't tell whether it went through. Check your email."}
        raise


def _submit_on_site(url: str, pdf, upload_name: str, contact: dict, doc_id: str, cover: dict | None,
                    make_cover: Callable[[], dict | None], job: dict, record: str, dry_run: bool) -> dict:
    """Runs on the browser thread. Open the posting's employer application, fill it and submit it.
    Returns {"kind": ..., "status", "note", "doc_id", "email", "cover_used"}."""
    from pathlib import Path

    from . import handshake as hs

    r = hs.employer_apply(url, Path(pdf), upload_name, contact, doc_id, cover)
    kind = r.get("kind")
    out = {"kind": kind, "status": "needs_manual", "note": r.get("message", ""), "doc_id": r.get("doc_id", ""),
           "email": r.get("email", ""), "cover_used": bool(r.get("cover"))}
    if kind == "handshake":
        return {**out, "status": "handshake"}
    if kind == "email":
        return {**out, "status": "email"}
    if kind == "account":
        return {**out, "note": "The employer's site wants you to sign in or create an account. That part is yours."}
    if kind not in ("form", "two-step", "page"):
        return {**out, "note": r.get("message") or "Couldn't open the employer's application."}
    page = next((p for p in hs._context().pages if _tid(hs, p) == r.get("target")), None)
    if page is None:
        return {**out, "note": "The employer's page closed before it could be filled."}
    if kind == "two-step":
        if r.get("cover_wanted") and not r.get("cover") and (cover := cover or make_cover()):
            c = hs._cover_in_box(page.locator("[role=dialog]").first, cover)
            out["cover_used"] = c.get("cover", False)
        box = page.locator("[role=dialog]").filter(has_text=re.compile(r"External Application", re.I)).first
        step2 = box.get_by_role("button", name=STEP2_RX)
        if not step2.count():
            step2 = box.get_by_role("link", name=STEP2_RX)
        if dry_run or not step2.count():
            return {**out, "status": "ready" if dry_run else "needs_manual",
                    "note": "Dry run: Step 1 is filled in on Handshake." if dry_run else "Couldn't find Step 2 in Handshake's application box."}
        try:
            with page.context.expect_page(timeout=15000) as info:
                step2.first.click()
            page = info.value
            page.wait_for_load_state("domcontentloaded")
        except Exception:
            return {**out, "note": "Handshake's Step 2 didn't open the employer's page. Finish this one yourself."}
        time.sleep(3)
    pdf_path = Path(pdf)

    def refill():
        nonlocal cover
        f = hs._fill_employer_page(page, pdf_path, upload_name, contact, cover)
        if f.get("cover_wanted") and not f.get("cover") and cover is None:
            cover = make_cover()
            if cover:
                f = hs._fill_employer_page(page, pdf_path, upload_name, contact, cover)
        out["cover_used"] = out["cover_used"] or bool(f.get("cover"))

    if r.get("cover_wanted") and not r.get("cover") or kind in ("two-step", "page"):
        refill()  # a CAPTCHA on the page stopped the first fill: the form itself is still filled in
    res = finish(page, job, record, dry_run, refill)
    out.update(status=res.status, note=res.note)
    if res.status == "applied":
        try:
            page.close()
        except Exception:
            pass
    return out


def _tid(hs, p) -> str:
    try:
        return hs._target_id(p)
    except Exception:
        return ""
