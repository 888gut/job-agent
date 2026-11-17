"""Handshake browser driver (Playwright, persistent profile, visible window).

All Playwright calls run on ONE dedicated thread (sync Playwright objects must stay on the
thread that created them). Use `run(fn, *args)` from anywhere to execute on that thread.

The agent never types passwords: you sign in yourself, from the dashboard's Handshake tab (a live
view of the off-screen sign-in window), and the session is kept in data/browser_profile. If Handshake shows a CAPTCHA, a sign-in page or a
form the agent doesn't understand, it stops and hands the job to you.
"""
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from .config import BROWSER_PROFILE, HANDSHAKE_BASE

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="handshake")

# Both Chrome windows run off-screen with the DevTools port open on 127.0.0.1 only, so the
# dashboard's Handshake tab can show them live (see live.py). The other flags keep an off-screen
# window drawing normally instead of being paused as "hidden".
DEBUG_PORT = 9333
VIEW_FLAGS = [f"--remote-debugging-port={DEBUG_PORT}", "--remote-debugging-address=127.0.0.1",
              "--window-position=-32000,-32000", "--window-size=1280,900",
              "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
              "--disable-background-timer-throttling", "--disable-features=CalculateNativeWinOcclusion"]
_state: dict = {"pw": None, "ctx": None}

JOB_ID_RX = re.compile(r"/(?:job-search|jobs|postings)/(\d{4,})")
BLOCKER_RX = re.compile(r"verify you are (a )?human|are you a robot|unusual traffic|captcha", re.I)
SUCCESS_RX = re.compile(r"application (was )?(submitted|sent)|you applied|successfully applied|applied on", re.I)


@dataclass
class ApplyResult:
    status: str          # applied | ready | needs_manual | blocked (CAPTCHA/sign-in: stop the whole run)
    note: str = ""
    doc_id: str = ""     # the Handshake document that was attached


def _with_retry(fn, *args):
    """If the agent's window was closed mid-task, reopen it and try once more."""
    try:
        return fn(*args)
    except Exception as e:
        if "has been closed" not in str(e) and "Target closed" not in str(e):
            raise
        _state["ctx"] = None
        return fn(*args)


def run(fn, *args):
    """Submit fn(*args) to the Playwright thread and return a Future."""
    return _executor.submit(_with_retry, fn, *args)


def _pause(lo: float = 2.0, hi: float = 5.0) -> None:
    time.sleep(random.uniform(lo, hi))


def _context():
    proc = _manual.get("proc")
    if proc is not None and proc.poll() is None:
        raise RuntimeError("Finish signing in first (Handshake tab > Done signing in); the agent can't use the browser while the sign-in window is open.")
    ctx = _state["ctx"]
    if ctx is not None:
        try:
            _ = ctx.pages  # raises if the browser was closed
            return ctx
        except Exception:
            _state["ctx"] = None
    from playwright.sync_api import sync_playwright

    if _state["pw"] is None:
        _state["pw"] = sync_playwright().start()
    # Use the installed, signed Chrome (or Edge): Windows can block Playwright's own browser
    # download. The agent always uses its own separate profile, never your everyday one.
    errors = []
    for channel in ("chrome", "msedge", None):
        try:
            # A normal Chrome window placed off-screen, so you don't see it. (Headless Chrome is
            # stopped by Handshake's bot check, and we don't try to get around that check.)
            ctx = _state["pw"].chromium.launch_persistent_context(
                str(BROWSER_PROFILE), channel=channel, headless=False, viewport={"width": 1280, "height": 900},
                args=VIEW_FLAGS,
            )
            ctx.on("close", lambda *_: _state.update(ctx=None))  # you closed the window -> reopen next time
            _state["ctx"] = ctx
            return ctx
        except Exception as e:
            errors.append(f"{channel or 'bundled'}: {str(e).splitlines()[0][:120]}")
    raise RuntimeError("Couldn't open a browser for Handshake. " + " | ".join(errors))


def _page():
    for _ in range(2):
        ctx = _context()
        try:
            return ctx.pages[0] if ctx.pages else ctx.new_page()
        except Exception:
            _state["ctx"] = None  # the window was closed; open a fresh one
    raise RuntimeError("Couldn't open the Handshake browser window.")


# Counts required questions left unanswered inside the application dialog: text boxes, dropdowns,
# checkboxes, radio groups, and custom widgets marked aria-required. Anything required that we
# didn't fill means the form needs a human.
UNANSWERED_JS = """(root) => {
  let n = 0;
  const seenRadio = new Set();
  for (const el of root.querySelectorAll('[required], [aria-required="true"]')) {
    const tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'file' || type === 'hidden' || el.disabled) continue;
    if (type === 'checkbox') { if (!el.checked) n++; continue; }
    if (type === 'radio') {
      const name = el.name || el.id;
      if (seenRadio.has(name)) continue;
      seenRadio.add(name);
      const group = el.name ? root.querySelectorAll('input[type=radio][name="' + CSS.escape(el.name) + '"]') : [el];
      if (![...group].some(r => r.checked)) n++;
      continue;
    }
    if (tag === 'input' || tag === 'textarea' || tag === 'select') { if (!String(el.value || '').trim()) n++; continue; }
    // custom widget (role=combobox/radiogroup/etc.) marked aria-required
    const picked = el.querySelector('[aria-checked="true"], [aria-selected="true"], input:checked');
    const text = (el.getAttribute('aria-valuetext') || el.value || el.textContent || '').trim();
    if (!picked && (!text || /^(select|choose|--)/i.test(text))) n++;
  }
  return n;
}"""


def _blocker(page) -> str | None:
    url = page.url.lower()
    if "/login" in url or "sso" in url or "accounts.google.com" in url:
        return "Handshake wants you to sign in again."
    for frame in page.frames:
        if any(k in frame.url for k in ("recaptcha", "hcaptcha", "turnstile", "challenges.cloudflare")):
            return "Handshake is showing a CAPTCHA."
    try:
        if BLOCKER_RX.search(page.inner_text("body", timeout=3000)[:5000]):
            return "Handshake is asking to verify you're human."
    except Exception:
        pass
    return None


def _raise_window(title_part: str = "Handshake") -> None:
    """Bring the agent's Chrome window to the front of the desktop (Windows only)."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, buf, 512)
                if title_part.lower() in buf.value.lower() and "Job Agent" not in buf.value:
                    found.append(hwnd)
            return True

        user32.EnumWindows(cb, 0)
        for hwnd in found[:1]:
            user32.ShowWindow(hwnd, 9)                                   # restore
            flags = 0x0001 | 0x0002 | 0x0040                             # no size/move, show
            user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags)             # topmost for a moment
            user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)             # then normal again
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _chrome_exe() -> str | None:
    import os
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as k:
            p = winreg.QueryValue(k, None)
            if p and os.path.exists(p):
                return p
    except OSError:
        pass
    for base in (os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""),
                 os.environ.get("LOCALAPPDATA", "")):
        p = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
        if p.exists():
            return str(p)
    return None


_manual: dict = {"proc": None}  # the normal (non-automated) sign-in Chrome, if open


def _forget_window_position() -> None:
    """Drop Chrome's saved window placement for the agent profile (it's off-screen)."""
    import json

    prefs = BROWSER_PROFILE / "Default" / "Preferences"
    try:
        data = json.loads(prefs.read_text(encoding="utf-8"))
        if data.get("browser", {}).pop("window_placement", None) is not None:
            prefs.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


PROFILE_NAME = "Job Agent"
PROFILE_COLOR = -14720336   # 0xFF1F62B0 (the dashboard's blue) as Chrome's signed ARGB int


def _label_profile() -> None:
    """Name the agent's Chrome profile "Job Agent" and give it a blue theme, so its window is
    clearly Google Chrome running the agent's profile, not your everyday one (Chrome's default
    name for a fresh profile is the confusing "Your Chrome"). Only edited while Chrome is closed."""
    import json

    def edit(path: Path, fn) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            fn(data)
            path.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass

    def local_state(d: dict) -> None:
        info = d.setdefault("profile", {}).setdefault("info_cache", {}).setdefault("Default", {})
        info.update({"name": PROFILE_NAME, "is_using_default_name": False,
                     "default_avatar_fill_color": PROFILE_COLOR, "profile_highlight_color": PROFILE_COLOR})

    def prefs(d: dict) -> None:
        d.setdefault("profile", {}).update({"name": PROFILE_NAME, "using_default_name": False})
        d.setdefault("browser", {}).setdefault("theme", {})["user_color"] = PROFILE_COLOR
        d.setdefault("autogenerated", {}).setdefault("theme", {})["color"] = PROFILE_COLOR

    edit(BROWSER_PROFILE / "Local State", local_state)
    edit(BROWSER_PROFILE / "Default" / "Preferences", prefs)


def _show_on_main_screen(pid: int, wait: float = 10.0) -> None:
    """Find the window Chrome opened for `pid`, move it onto the main screen, maximize it and
    bring it to the front (Windows only)."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        deadline = time.time() + wait
        while time.time() < deadline:
            found = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def cb(hwnd, _):
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                if owner.value == pid and user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd):
                    found.append(hwnd)
                return True

            user32.EnumWindows(cb, 0)
            if found:
                hwnd = found[0]
                user32.ShowWindow(hwnd, 9)                                    # restore
                user32.SetWindowPos(hwnd, 0, 60, 40, 1280, 900, 0x0040)       # onto the main screen
                user32.ShowWindow(hwnd, 3)                                    # maximize
                user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0043)             # front...
                user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0043)             # ...but not always-on-top
                # Windows ignores SetForegroundWindow from a background process (the hidden server),
                # which left this window behind whatever you were looking at. A synthetic Alt tap and
                # briefly sharing input with the current foreground thread lift that restriction.
                fg = user32.GetForegroundWindow()
                fg_thread = user32.GetWindowThreadProcessId(fg, None)
                me = ctypes.windll.kernel32.GetCurrentThreadId()
                user32.keybd_event(0x12, 0, 0, 0)                             # Alt down
                user32.keybd_event(0x12, 0, 2, 0)                             # Alt up
                attached = fg_thread and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True)
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                if attached:
                    user32.AttachThreadInput(me, fg_thread, False)
                if user32.GetForegroundWindow() != hwnd:
                    user32.FlashWindow(hwnd, True)                            # at least flash the taskbar button
                return
            time.sleep(0.4)
    except Exception:
        pass


# ---------------- public operations (call through run()) ----------------
def open_login() -> str:
    """Open Handshake's sign-in in a NORMAL Chrome window (no automation attached) on the agent's
    own profile. Sign-in pages such as Google/school SSO often refuse automated browsers.
    Sign in, close that window, and the agent reuses the saved session."""
    import subprocess

    close()  # the profile can only be open in one Chrome at a time
    _quit_any_browser()
    exe = _chrome_exe()
    if not exe:
        raise RuntimeError("Google Chrome wasn't found on this PC.")
    # Chrome remembers the last window position per profile, and the agent's background window
    # lives off-screen, so forget that position and then put this window on your main screen.
    _forget_window_position()
    _label_profile()
    _manual["proc"] = subprocess.Popen([
        exe, f"--user-data-dir={BROWSER_PROFILE}", "--no-first-run", "--no-default-browser-check",
        *VIEW_FLAGS, "--new-window", f"{HANDSHAKE_BASE}/login",
    ])
    return "Handshake's sign-in is open below. Sign in, then click Done signing in."


def _browser_ws() -> str | None:
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=1.5) as r:
            return json.load(r)["webSocketDebuggerUrl"]
    except Exception:
        return None


def _quit_any_browser(wait: float = 15.0) -> None:
    """Make sure no Chrome is still using the agent's profile (one may be mid-shutdown). A new
    window started while one is still running just joins it, which breaks the sign-in window."""
    import json

    end = time.time() + wait
    asked = False
    while time.time() < end:
        ws_url = _browser_ws()
        if not ws_url:
            time.sleep(0.5)          # let Chrome release the profile lock
            return
        if not asked or time.time() > end - wait / 2:
            try:
                from websockets.sync.client import connect

                with connect(ws_url, open_timeout=3) as ws:
                    ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
                asked = True
            except Exception:
                pass
        time.sleep(0.5)
    raise RuntimeError("The agent's browser didn't close in time. Try Sign in to Handshake again in a moment.")


def login_open() -> bool:
    proc = _manual.get("proc")
    return proc is not None and proc.poll() is None


def finish_login() -> str:
    """Close the sign-in window the normal way (so Chrome saves the session), then let the agent
    use the profile again."""
    proc = _manual.get("proc")
    try:  # ask Chrome to quit cleanly, like closing the window, so it saves the session
        _quit_any_browser()
    except Exception:
        pass
    if proc is not None and proc.poll() is None:
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.terminate()
    _manual["proc"] = None
    return "Signed-in session saved. The agent can use Handshake again."


def pop_out() -> str:
    """Bring the sign-in window onto your screen, for anything the live view can't do (a passkey
    prompt, a file picker)."""
    proc = _manual.get("proc")
    if proc is None or proc.poll() is not None:
        return "The sign-in window isn't open."
    _show_on_main_screen(proc.pid)
    return "The sign-in window is on your screen now. Click Done signing in here when you're finished."


def is_logged_in() -> bool:
    page = _page()
    page.goto(f"{HANDSHAKE_BASE}/job-search", wait_until="domcontentloaded")
    _pause(2, 3)
    return _blocker(page) is None and "/login" not in page.url


CARD_JS = """els => els.map(e => ({hook: e.getAttribute('data-hook') || '', text: e.innerText || ''}))"""


def search(query: str) -> list[tuple[str, str, str]]:
    """Return [(handshake_id, url, card_text)] in Handshake's relevance order.

    Handshake's location filters aren't URL parameters, so location is filtered afterwards
    from each result card's text (see pipeline.location_ok).
    """
    page = _page()
    page.goto(f"{HANDSHAKE_BASE}/job-search?query={quote_plus(query)}&per_page=50", wait_until="domcontentloaded")
    _pause(4, 6)
    block = _blocker(page)
    if block:
        raise RuntimeError(block)
    for _ in range(4):  # results load lazily
        page.mouse.wheel(0, 2500)
        _pause(1, 2)
    found: dict[str, tuple[str, str, str]] = {}
    for card in page.eval_on_selector_all('[data-hook^="job-result-card |"]', CARD_JS):
        m = re.search(r"(\d{4,})", card["hook"])
        if m and m.group(1) not in found:
            jid = m.group(1)
            found[jid] = (jid, f"{HANDSHAKE_BASE}/jobs/{jid}", card["text"])
    if not found:  # fallback: any job links on the page
        for h in page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)"):
            m = JOB_ID_RX.search(h)
            if m and m.group(1) not in found:
                found[m.group(1)] = (m.group(1), f"{HANDSHAKE_BASE}/jobs/{m.group(1)}", "")
    return list(found.values())


def read_posting(url: str) -> str:
    page = _page()
    page.goto(url, wait_until="domcontentloaded")
    _pause(2.5, 4.5)
    block = _blocker(page)
    if block:
        raise RuntimeError(block)
    for sel in ("main", "[role=main]", "body"):
        try:
            return page.inner_text(sel, timeout=5000)
        except Exception:
            continue
    return ""


def open_for_user(url: str) -> None:
    """Open a posting in your regular browser (the agent's own browser is invisible)."""
    import webbrowser

    webbrowser.open(url)


# Fills only your contact basics on an employer's form, by matching each empty field's label.
# Never touches password fields, checkboxes, radio buttons, dropdowns or free-text questions.
FILL_JS = r"""(c) => {
  const set = (el, v) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, v);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const labelOf = (el) => {
    let t = [el.name, el.id, el.placeholder, el.getAttribute('aria-label'), el.getAttribute('autocomplete')].join(' ');
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) t += ' ' + l.innerText; }
    const p = el.closest('label'); if (p) t += ' ' + p.innerText;
    return t.toLowerCase();
  };
  const rules = [
    ['first name', /first.?name|given.?name|fname/, c.first],
    ['last name', /last.?name|family.?name|surname|lname/, c.last],
    ['email', /e-?mail/, c.email],
    ['phone', /phone|mobile|\btel\b/, c.phone],
    ['name', /full.?name|your.?name|\bname\b/, c.name],
  ];
  const filled = [];
  const sel = 'input:not([type]), input[type=text], input[type=email], input[type=tel]';
  for (const el of document.querySelectorAll(sel)) {
    if (!el.offsetParent || el.value || el.disabled || el.readOnly) continue;
    const l = labelOf(el);
    if (/user.?name|login|password|search|company|employer|school|reference|referr/.test(l)) continue;
    for (const [key, rx, val] of rules) {
      if (val && rx.test(l)) { set(el, val); filled.push(key); break; }
    }
  }
  return [...new Set(filled)];
}"""


def _target_id(page) -> str:
    return page.context.new_cdp_session(page).send("Target.getTargetInfo")["targetInfo"]["targetId"]


def _fill_employer_page(emp, resume_pdf: Path, upload_name: str | None, contact: dict) -> dict:
    """Fill contact basics and attach the resume on an employer page. Never clicks Submit."""
    out = {"filled": [], "resume": False, "account": False, "email": ""}
    mails = [m for m in emp.eval_on_selector_all("a[href^='mailto:']", "els => els.map(e => e.href)") if "@" in m]
    if mails:
        out["email"] = mails[0][7:].split("?")[0]
    for frame in emp.frames:  # some job forms (e.g. Greenhouse) sit inside an iframe
        try:
            if frame.locator("input[type=password]").count() and any(
                    frame.locator("input[type=password]").nth(i).is_visible()
                    for i in range(frame.locator("input[type=password]").count())):
                out["account"] = True  # a sign-in / create-account page: that part is yours
                continue
            out["filled"] += frame.evaluate(FILL_JS, contact)
        except Exception:
            continue
    out["filled"] = list(dict.fromkeys(out["filled"]))
    if not out["account"]:
        for frame in emp.frames:
            try:
                files = frame.locator("input[type=file]")
                n = files.count()
                if not n:
                    continue
                pick = 0
                for i in range(n):  # prefer the resume/CV field over cover letter or transcript
                    hint = (files.nth(i).evaluate("e => [e.name, e.id, e.accept, e.getAttribute('aria-label'), "
                                                  "(e.closest('label,div,fieldset')||{}).innerText||''].join(' ')") or "").lower()
                    if re.search(r"resume|cv\b|curriculum", hint):
                        pick = i
                        break
                else:
                    if n > 1:
                        continue  # several upload boxes and none says resume: leave it to you
                files.nth(pick).set_input_files(str(_upload_copy(Path(resume_pdf), upload_name)))
                out["resume"] = True
                break
            except Exception:
                continue
    return out


def employer_apply(url: str, resume_pdf: Path, upload_name: str | None, contact: dict, doc_id: str = "") -> dict:
    """Open a posting's 'Apply externally' page in a new tab of the agent's browser, fill your
    contact basics and attach your resume where that's safe, and leave it for you to finish.
    It never creates accounts, types passwords, answers questions or clicks Submit."""
    page = _page()
    page.goto(url, wait_until="domcontentloaded")
    _pause(3, 5)
    if (b := _blocker(page)):
        return {"kind": "blocked", "message": b}
    _wait_for_apply_buttons(page)
    if page.get_by_role("button", name=APPLY_BTN_RX).count():
        return {"kind": "handshake"}
    ext = page.get_by_role("button", name=EXTERNAL_RX)
    if not ext.count():
        ext = page.get_by_role("link", name=EXTERNAL_RX)
    if not ext.count():
        return {"kind": "none", "message": "Couldn't find the Apply externally button on the posting."}
    emp = None
    try:
        with page.context.expect_page(timeout=8000) as info:
            ext.first.click()
        emp = info.value
        try:
            emp.wait_for_url(lambda u: u not in ("", "about:blank"), timeout=20000)
        except Exception:
            pass
        emp.wait_for_load_state("domcontentloaded")
    except Exception:
        if "joinhandshake.com" not in page.url:  # opened in the same tab instead
            emp = page
    if emp is None:
        # Some postings open a two-step box instead: Step 1 sends documents through Handshake,
        # Step 2 opens the employer's page. Clicking Step 2 submits Step 1, so that click is yours.
        box = page.locator("[role=dialog]").filter(has_text=re.compile(r"External Application", re.I))
        if box.count():
            scope = box.first
            wants_doc = scope.locator("input[type=file]").count() or scope.locator("button.rosetta-inline-alert__close-button").count()
            problem = _attach_resume(scope, resume_pdf, upload_name, doc_id) if wants_doc else None
            return {"kind": "two-step", "target": _target_id(page), "url": page.url, "filled": [],
                    "resume": bool(wants_doc) and problem is None, "message": problem or "",
                    "doc_id": _attached_doc_id(scope) if wants_doc and problem is None else ""}
        return {"kind": "none", "message": "The employer's application page didn't open."}
    _pause(3, 5)
    result = {"kind": "form", "url": emp.url, "target": _target_id(emp)}
    if (b := _blocker(emp)):
        return {**result, "kind": "page", "message": b, "filled": [], "resume": False}
    filled = _fill_employer_page(emp, resume_pdf, upload_name, contact)
    result.update(filled)
    if filled["email"]:
        result["kind"] = "email"
    elif filled["account"]:
        result["kind"] = "account"
    return result


def employer_fill(target_id: str, resume_pdf: Path, upload_name: str | None, contact: dict) -> dict:
    """Fill again on the employer tab you're looking at (after you signed in, or on the next page)."""
    pages = _context().pages
    for p in pages:
        try:
            if target_id and _target_id(p) == target_id and "joinhandshake.com" not in p.url:
                return {**_fill_employer_page(p, resume_pdf, upload_name, contact), "target": target_id}
        except Exception:
            continue
    for p in reversed(pages):  # newest tab that isn't Handshake
        if "joinhandshake.com" not in p.url and p.url not in ("", "about:blank"):
            return {**_fill_employer_page(p, resume_pdf, upload_name, contact), "target": _target_id(p)}
    raise RuntimeError("The employer's page isn't open yet. Click Step 2 on Handshake (or Apply on employer site) first.")


LIST_DOCS_JS = r"""() => [...document.querySelectorAll('tr')].map(tr => {
  const a = tr.querySelector('a[href*="/docs/"]'); if (!a) return null;
  const m = (a.getAttribute('href') || '').match(/\/docs\/(\d+)/); if (!m) return null;
  return {id: m[1], name: a.innerText.trim(), row: tr.innerText.replace(/\s+/g, ' ').trim()};
}).filter(Boolean)"""


def list_docs() -> list[dict]:
    """Your Handshake documents: [{id, name, row}] (row text includes the upload date)."""
    page = _context().new_page()
    out: dict[str, dict] = {}
    try:
        for n in range(1, 11):
            page.goto(f"{HANDSHAKE_BASE}/docs?page={n}", wait_until="domcontentloaded")
            _pause(3, 4)
            if (b := _blocker(page)):
                raise RuntimeError(b)
            rows = page.evaluate(LIST_DOCS_JS)
            fresh = [r for r in rows if r["id"] not in out]
            if not fresh:
                break
            out.update({r["id"]: r for r in fresh})
    finally:
        page.close()
    return list(out.values())


def delete_doc(doc_id: str) -> bool:
    """Delete one of your Handshake documents (Edit > Delete). Only called for resumes the agent
    itself uploaded that haven't been used for a week and were never sent with an application."""
    page = _context().new_page()
    try:
        page.goto(f"{HANDSHAKE_BASE}/docs/{doc_id}/edit", wait_until="domcontentloaded")
        _pause(3, 4)
        if _blocker(page):
            return False
        btn = page.get_by_role("button", name=re.compile(r"^\s*delete\s*$", re.I))
        if not btn.count():
            return False
        page.once("dialog", lambda d: d.accept())  # a browser "Are you sure?" prompt, if Handshake uses one
        btn.first.click()
        _pause(1.5, 2.5)
        confirm = page.get_by_role("dialog").get_by_role("button", name=re.compile(r"delete|confirm|yes", re.I))
        if confirm.count():
            confirm.first.click()
            _pause(2, 3)
        page.goto(f"{HANDSHAKE_BASE}/docs/{doc_id}", wait_until="domcontentloaded")
        _pause(2, 3)
        gone = "/docs/" + doc_id not in page.url or bool(re.search(r"not found|doesn.t exist|no longer", page.inner_text("body")[:3000], re.I))
        return gone
    finally:
        page.close()


APPLY_BTN_RX = re.compile(r"^\s*(quick\s+|easy\s+)?apply(\s+now)?\s*$", re.I)
EXTERNAL_RX = re.compile(r"apply\s+(externally|on\s+(company|employer)|on\s+.*site)", re.I)


def _upload_copy(resume_pdf: Path, upload_name: str | None) -> Path:
    """The employer sees the uploaded file's name, so upload a copy with a clean name
    (e.g. First_Last_Resume.pdf) instead of an internal one like tailored_3.pdf."""
    if not upload_name:
        return resume_pdf
    out_dir = BROWSER_PROFILE.parent / "uploads"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", upload_name).strip("_") or "Resume.pdf"
    if not safe.lower().endswith(".pdf"):
        safe += ".pdf"
    dest = out_dir / safe
    dest.write_bytes(Path(resume_pdf).read_bytes())
    return dest


# Handshake's resume picker keeps document ids in a hidden <select>; the visible option at the same
# position (id ending "-select-<n>") is the one to click. Returns that option's element id.
DOC_OPTION_JS = r"""(root, docId) => {
  const sel = [...root.querySelectorAll('select')].find(s => [...s.options].some(o => o.value === docId));
  if (!sel) return null;
  const i = [...sel.options].findIndex(o => o.value === docId);
  let box = sel.parentElement;
  for (let k = 0; k < 8 && box && !box.querySelector('[role=option]'); k++) box = box.parentElement;
  const opt = box && [...box.querySelectorAll('[role=option]')].find(o => o.id.endsWith('-select-' + i));
  return opt ? opt.id : null;
}"""


def _attached_doc_id(scope) -> str:
    link = scope.locator("a[href*='/docs/']")
    for i in range(link.count()):
        m = re.search(r"/docs/(\d+)", link.nth(i).get_attribute("href") or "")
        if m:
            return m.group(1)
    return ""


def _attach_resume(scope, resume_pdf: Path, upload_name: str | None, doc_id: str = "") -> str | None:
    """In a Handshake application box: remove the resume Handshake pre-attached from your profile,
    then attach ours: pick it from your Handshake documents when it was uploaded before (doc_id),
    otherwise upload it and wait until Handshake has processed it. Returns a problem, or None.
    Read the attached document's id afterwards with _attached_doc_id(scope)."""
    upload = _upload_copy(Path(resume_pdf), upload_name)
    attached = scope.locator("button.rosetta-inline-alert__close-button")
    if attached.count():
        attached.first.click()
        _pause(1.5, 2.5)
    if doc_id:  # reuse the copy already in your Handshake documents
        try:
            opt_id = scope.evaluate(DOC_OPTION_JS, doc_id)
            if opt_id:
                scope.locator(f"[id='{opt_id}']").first.click()
                deadline = time.time() + 20
                while time.time() < deadline:
                    if _attached_doc_id(scope) == doc_id:
                        return None
                    time.sleep(1)
        except Exception:
            pass  # fall back to uploading it
        attached = scope.locator("button.rosetta-inline-alert__close-button")
        if attached.count():
            attached.first.click()
            _pause(1, 2)
    file_inputs = scope.locator("input[type=file]")
    if file_inputs.count() == 0:
        return "No resume upload option found in the application form."
    file_inputs.first.set_input_files(str(upload))
    # wait until Handshake shows our file as attached AND has finished processing it
    deadline = time.time() + 90
    while time.time() < deadline:
        text = scope.inner_text()
        busy = re.search(r"converting|uploading|processing", text, re.I)
        if upload.name in text and not busy and scope.locator("button.rosetta-inline-alert__close-button").count():
            return None
        time.sleep(1.5)
    return "Handshake didn't finish processing the resume upload. Check this job on Handshake."


def _wait_for_apply_buttons(page, wait: float = 20.0) -> None:
    """Postings render their Apply buttons a few seconds after the page loads (longer right after
    the browser starts). Wait for one of them instead of deciding too early."""
    end = time.time() + wait
    applied_rx = re.compile(r"^\s*applied\b|withdraw", re.I)
    while time.time() < end:
        if (page.get_by_role("button", name=APPLY_BTN_RX).count() or page.get_by_role("button", name=EXTERNAL_RX).count()
                or page.get_by_role("link", name=EXTERNAL_RX).count() or page.get_by_role("button", name=applied_rx).count()):
            return
        time.sleep(1)


def apply(url: str, resume_pdf: Path, dry_run: bool = True, upload_name: str | None = None, doc_id: str = "") -> ApplyResult:
    """Open the posting, open its Handshake application form, swap in our resume and (unless
    dry_run) click Submit Application.

    Anything unexpected -> needs_manual, so you can finish that one yourself.
    """
    page = _page()
    page.goto(url, wait_until="domcontentloaded")
    _pause(3, 5)
    if (b := _blocker(page)):
        return ApplyResult("blocked", b)

    buttons = page.get_by_role("button", name=APPLY_BTN_RX)
    _wait_for_apply_buttons(page)
    if buttons.count() == 0:
        if page.get_by_role("link", name=EXTERNAL_RX).count() or page.get_by_role("button", name=EXTERNAL_RX).count():
            return ApplyResult("needs_manual", "This job is applied for on the employer's own site.")
        if page.get_by_role("button", name=re.compile(r"^\s*applied\b|withdraw", re.I)).count() \
                or SUCCESS_RX.search(page.inner_text("body")[:8000]):
            return ApplyResult("applied", "Handshake already shows this job as applied.")
        return ApplyResult("needs_manual", "Couldn't find an Apply button.")

    buttons.first.click()
    _pause(2, 4)
    if (b := _blocker(page)):
        return ApplyResult("blocked", b)

    dialog = page.get_by_role("dialog")
    if dialog.count() == 0:
        return ApplyResult("needs_manual", "The application form didn't open.")
    scope = dialog.first
    if (problem := _attach_resume(scope, resume_pdf, upload_name, doc_id)):
        return ApplyResult("needs_manual", problem)
    doc_id = _attached_doc_id(scope)
    _pause(1.5, 2.5)

    # Extra required questions/documents we can't answer safely -> hand off to you.
    try:
        unanswered = scope.evaluate(UNANSWERED_JS)
    except Exception:
        unanswered = -1  # couldn't check: don't guess
    if unanswered:
        return ApplyResult("needs_manual", "The form has extra required questions. Open the posting and apply yourself (the tailored resume is ready to download).")
    if scope.locator("input[type=file]").count() > 1:
        return ApplyResult("needs_manual", "The form asks for more documents (e.g. transcript or cover letter).")

    submit = scope.get_by_role("button", name=re.compile(r"submit", re.I))
    if submit.count() == 0:
        return ApplyResult("needs_manual", "Couldn't find the Submit button.")
    if dry_run:
        return ApplyResult("ready", "Dry run passed: everything was filled in and the agent stopped before Submit. To send it for real, turn off dry run in Settings, then click Approve & Submit.", doc_id)

    submit.first.click()
    _pause(4, 6)
    if (b := _blocker(page)):
        return ApplyResult("blocked", f"{b} It may or may not have been submitted. Check this job on Handshake.")
    if SUCCESS_RX.search(page.inner_text("body")[:10000]):
        return ApplyResult("applied", "Submitted on Handshake.", doc_id)
    # Check the posting itself: once applied, Handshake swaps the Apply button for an Applied state.
    page.reload(wait_until="domcontentloaded")
    _pause(3, 5)
    body = page.inner_text("body")[:10000]
    if page.get_by_role("button", name=APPLY_BTN_RX).count() == 0 and (
            SUCCESS_RX.search(body) or re.search(r"\bapplied\b|withdraw", body, re.I)):
        return ApplyResult("applied", "Submitted on Handshake.", doc_id)
    return ApplyResult("needs_manual", "Clicked Submit but couldn't confirm it went through. Check this job on Handshake.", doc_id)


def close() -> None:
    try:
        if _state["ctx"]:
            _state["ctx"].close()
        if _state["pw"]:
            _state["pw"].stop()
    finally:
        _state["ctx"] = _state["pw"] = None
