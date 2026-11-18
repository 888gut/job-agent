"""Applying on employers' own sites."""
import pytest

from app import employer


FORM = """<!doctype html><html><body>
<form onsubmit="event.preventDefault(); if (!document.getElementById('why').value) { document.getElementById('err').textContent = 'This field is required'; return; }
  document.body.innerHTML = '<h1>Thank you for applying!</h1>';">
  <label for="fn">First name *</label><input id="fn" required>
  <label for="why">Why are you interested? *</label><textarea id="why" required></textarea>
  <label for="src">How did you hear about us?</label><select id="src"><option></option><option>LinkedIn</option><option>Handshake</option></select>
  <fieldset><legend>Gender</legend>
    <label><input type="radio" name="g" value="m"> Male</label>
    <label><input type="radio" name="g" value="d"> I don't wish to answer</label></fieldset>
  <label><input type="checkbox" id="cert"> I certify this information is accurate</label>
  <label><input type="checkbox" id="news"> Send me marketing emails</label>
  <span id="err"></span>
  <button type="submit">Submit application</button>
</form></body></html>"""


@pytest.fixture
def page():
    pw = pytest.importorskip("playwright.sync_api").sync_playwright().start()
    try:
        browser = pw.chromium.launch(channel="chrome", headless=True)
    except Exception:
        pw.stop()
        pytest.skip("Chrome isn't available for a headless test")
    p = browser.new_page()
    p.set_content(FORM)
    yield p
    browser.close()
    pw.stop()


def _answers(values):
    def answer(fields, job, record):
        seen = {f["label"]: f for f in fields}
        out = []
        for f in fields:
            for key, val in values.items():
                if key in f["label"]:
                    out.append(employer.Answer(id=f["id"], value=val))
        answer.fields = seen
        return employer.Answers(answers=out, cannot_answer=[])
    return answer


def test_employer_form_is_filled_and_submitted(page):
    ans = _answers({"First name": "Sam", "interested": "I like logistics.", "hear about": "Handshake",
                    "Gender": "I don't wish to answer", "certify": "check"})
    res = employer.finish(page, {"title": "Analyst", "company": "Acme"}, "record", dry_run=False,
                          refill=lambda: None, answer=ans)
    assert res.status == "applied", res.note
    assert any("First name" in k for k in ans.fields)
    assert any(f["kind"] == "radio" and "I don't wish to answer" in f["options"] for f in ans.fields.values())


def test_employer_form_dry_run_stops_before_submit(page):
    res = employer.finish(page, {}, "", dry_run=True, refill=lambda: None,
                          answer=_answers({"First name": "Sam", "interested": "x"}))
    assert res.status == "ready"
    assert page.locator("#fn").input_value() == "Sam"
    assert not page.locator("#news").is_checked()


def test_employer_form_stops_when_a_required_answer_is_unknown(page):
    def answer(fields, job, record):
        return employer.Answers(answers=[], cannot_answer=["Why are you interested?"])
    res = employer.finish(page, {}, "", dry_run=False, refill=lambda: None, answer=answer)
    assert res.status == "needs_manual" and "Why are you interested" in res.note
    assert "Thank you" not in page.inner_text("body")


def test_employer_site_never_handles_passwords(page):
    page.set_content('<form><label>Email<input></label><label>Password<input type="password"></label><button>Submit</button></form>')
    res = employer.finish(page, {}, "", dry_run=False, refill=lambda: None,
                          answer=lambda *a: pytest.fail("must not answer a sign-in form"))
    assert res.status == "needs_manual" and "sign in" in res.note


def test_hidden_background_recaptcha_is_not_a_captcha_but_a_visible_one_is(page):
    page.set_content('<p>This site is protected by reCAPTCHA.</p>'
                     '<iframe src="about:blank#recaptcha/api2/anchor?size=invisible" style="width:256px;height:60px"></iframe>')
    assert employer.challenge_on_page(page) is None
    page.set_content('<iframe name="c" style="width:304px;height:78px"></iframe>')
    page.frames[1].goto("about:blank#recaptcha/api2/anchor")
    assert employer.challenge_on_page(page)


def test_forms_inside_web_components_are_read_and_filled(page):
    page.set_content("""<x-form></x-form><script>
      customElements.define('x-form', class extends HTMLElement { connectedCallback() {
        const r = this.attachShadow({mode: 'open'});
        r.innerHTML = '<label for="ph">Phone number *</label><input id="ph" required><button>Next</button>';
        r.querySelector('button').onclick = () => { document.body.innerHTML = '<p>Thank you for applying</p>'; };
      }});</script>""")
    seen = []

    def answer(fields, job, record):
        seen.extend(fields)
        return employer.Answers(answers=[employer.Answer(id=fields[0]["id"], value="555-0100")], cannot_answer=[])
    res = employer.finish(page, {}, "", dry_run=False, refill=lambda: None, answer=answer)
    assert [f["label"] for f in seen] == ["Phone number *"]
    assert res.status == "applied"


def test_bot_check_pages_go_to_you(page):
    page.set_content('<iframe name="dd" style="width:400px;height:400px"></iframe>')
    page.frames[1].goto("about:blank#geo.captcha-delivery.com/captcha/")
    res = employer.finish(page, {}, "", dry_run=False, refill=lambda: None,
                          answer=lambda *a: pytest.fail("must not fill a CAPTCHA page"))
    assert res.status == "needs_manual" and "CAPTCHA" in res.note
