"""Handshake browser driver (Playwright, persistent profile, visible window).

All Playwright calls run on ONE dedicated thread (sync Playwright objects must stay on the
thread that created them). Use `run(fn, *args)` from anywhere to execute on that thread.

The agent never types passwords: you sign in yourself once in the window it opens, and the
session is kept in data/browser_profile. If Handshake shows a CAPTCHA, a sign-in page or a
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
_state: dict = {"pw": None, "ctx": None}

JOB_ID_RX = re.compile(r"/(?:job-search|jobs|postings)/(\d{4,})")
BLOCKER_RX = re.compile(r"verify you are (a )?human|are you a robot|unusual traffic|captcha", re.I)
SUCCESS_RX = re.compile(r"application (was )?(submitted|sent)|you applied|successfully applied|applied on", re.I)


@dataclass
class ApplyResult:
    status: str          # applied | ready | needs_manual | failed
    note: str = ""


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
        raise RuntimeError("Close the Handshake sign-in window first; the agent can't use the browser while it's open.")
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
                args=["--window-position=-32000,-32000", "--window-size=1280,900"],
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


# ---------------- public operations (call through run()) ----------------
def open_login() -> str:
    """Open Handshake's sign-in in a NORMAL Chrome window (no automation attached) on the agent's
    own profile. Sign-in pages such as Google/school SSO often refuse automated browsers.
    Sign in, close that window, and the agent reuses the saved session."""
    import subprocess

    close()  # the profile can only be open in one Chrome at a time
    exe = _chrome_exe()
    if not exe:
        raise RuntimeError("Google Chrome wasn't found on this PC.")
    _manual["proc"] = subprocess.Popen([
        exe, f"--user-data-dir={BROWSER_PROFILE}", "--no-first-run", "--no-default-browser-check",
        "--new-window", f"{HANDSHAKE_BASE}/login",
    ])
    time.sleep(3)
    _raise_window()
    return "Sign in to Handshake in the Chrome window that opened, then close that window."


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


def apply(url: str, resume_pdf: Path, dry_run: bool = True) -> ApplyResult:
    """Open the posting, attach the resume and (unless dry_run) click Submit.

    Anything unexpected -> needs_manual, so you can finish that one yourself.
    """
    page = _page()
    page.goto(url, wait_until="domcontentloaded")
    _pause(3, 5)
    if (b := _blocker(page)):
        return ApplyResult("needs_manual", b)

    apply_btn = page.get_by_role("button", name=re.compile(r"^\s*apply", re.I))
    if apply_btn.count() == 0:
        if page.get_by_role("link", name=re.compile(r"apply", re.I)).count():
            return ApplyResult("needs_manual", "This job is applied for on the employer's own site.")
        if SUCCESS_RX.search(page.inner_text("body")[:8000]):
            return ApplyResult("applied", "Handshake already shows this job as applied.")
        return ApplyResult("needs_manual", "Couldn't find an Apply button.")
    label = (apply_btn.first.inner_text() or "").lower()
    if "external" in label:
        return ApplyResult("needs_manual", "This job is applied for on the employer's own site.")

    apply_btn.first.click()
    _pause(2, 4)
    if (b := _blocker(page)):
        return ApplyResult("needs_manual", b)

    dialog = page.get_by_role("dialog")
    scope = dialog.first if dialog.count() else page

    file_inputs = scope.locator("input[type=file]")
    if file_inputs.count():
        file_inputs.first.set_input_files(str(resume_pdf))
        _pause(3, 5)
    else:
        return ApplyResult("needs_manual", "No resume upload field found in the application form.")

    # Extra required questions/documents we can't answer safely -> hand off to you.
    empty_required = scope.locator(
        "textarea[required], input[required]:not([type=file]):not([type=hidden]), select[required]"
    )
    for i in range(empty_required.count()):
        try:
            if not empty_required.nth(i).input_value().strip():
                return ApplyResult("needs_manual", "The form has extra required questions. Open the posting and apply yourself (the tailored resume is ready to download).")
        except Exception:
            continue
    if scope.locator("input[type=file]").count() > 1:
        return ApplyResult("needs_manual", "The form asks for more documents (e.g. transcript or cover letter).")

    submit = scope.get_by_role("button", name=re.compile(r"submit", re.I))
    if submit.count() == 0:
        return ApplyResult("needs_manual", "Couldn't find the Submit button.")
    if dry_run:
        return ApplyResult("ready", "Dry run: everything was filled in and the agent stopped before Submit. Turn off dry run in Settings to submit for real.")

    submit.first.click()
    _pause(3, 5)
    if (b := _blocker(page)):
        return ApplyResult("needs_manual", b)
    if SUCCESS_RX.search(page.inner_text("body")[:10000]):
        return ApplyResult("applied", "Submitted on Handshake.")
    return ApplyResult("needs_manual", "Clicked Submit but couldn't confirm it went through. Check this job on Handshake.")


def close() -> None:
    try:
        if _state["ctx"]:
            _state["ctx"].close()
        if _state["pw"]:
            _state["pw"].stop()
    finally:
        _state["ctx"] = _state["pw"] = None
