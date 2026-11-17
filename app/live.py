"""Live view of the Handshake browser, shown in the dashboard's Handshake tab.

Both of the agent's Chrome windows (the automated one it applies with, and the plain sign-in
window) are started off-screen with Chrome's DevTools port open on 127.0.0.1 only. This module
talks to that port directly to:

  * take a screenshot of the current tab (the dashboard polls it a few times a second), and
  * pass your clicks, scrolling and typing into that tab, so you can sign in from the dashboard.

Nothing here logs what you type. Input is refused while the agent is searching or applying, so
you can watch a run but not interfere with it.
"""
import base64
import json
import threading
import urllib.request

from websockets.sync.client import connect

from .handshake import DEBUG_PORT

_BASE = f"http://127.0.0.1:{DEBUG_PORT}"
_lock = threading.Lock()
_conn: dict = {"ws": None, "target": None, "id": 0, "want": None, "size": (1280, 900), "seen": set()}

_HIDDEN = ("devtools://", "chrome://", "chrome-extension://", "chrome-untrusted://", "edge://")

# DevTools key descriptions for the non-text keys the dashboard forwards.
_KEYS = {
    "Enter": ("Enter", 13, "\r"), "Backspace": ("Backspace", 8, None), "Tab": ("Tab", 9, None),
    "Escape": ("Escape", 27, None), "Delete": ("Delete", 46, None), "Home": ("Home", 36, None),
    "End": ("End", 35, None), "ArrowLeft": ("ArrowLeft", 37, None), "ArrowUp": ("ArrowUp", 38, None),
    "ArrowRight": ("ArrowRight", 39, None), "ArrowDown": ("ArrowDown", 40, None),
    "PageUp": ("PageUp", 33, None), "PageDown": ("PageDown", 34, None),
}


class NoBrowser(Exception):
    """No Handshake browser is open right now."""


def tabs() -> list[dict]:
    """Open tabs in the Handshake browser, most recent first. Empty if no browser is open."""
    try:
        with urllib.request.urlopen(f"{_BASE}/json/list", timeout=1.5) as r:
            items = json.load(r)
    except Exception:
        return []
    return [{"id": t["id"], "title": t.get("title", ""), "url": t.get("url", ""), "ws": t.get("webSocketDebuggerUrl", "")}
            for t in items if t.get("type") == "page" and not t.get("url", "").startswith(_HIDDEN)]


def _drop() -> None:
    ws = _conn["ws"]
    _conn["ws"] = _conn["target"] = None
    if ws is not None:
        try:
            ws.close()
        except Exception:
            pass


def _pick(open_tabs: list[dict]) -> dict:
    if not open_tabs:
        _conn["seen"] = set()
        raise NoBrowser()
    ids = {t["id"] for t in open_tabs}
    fresh = [t for t in open_tabs if t["id"] not in _conn["seen"]]
    if _conn["seen"] and fresh:  # a tab just opened (e.g. the employer's page): show it
        _conn["want"] = fresh[0]["id"]
    _conn["seen"] = ids
    for t in open_tabs:
        if t["id"] == _conn["want"]:
            return t
    for t in open_tabs:  # a tab that's actually on Handshake (or its sign-in) beats a blank one
        if t["url"] not in ("", "about:blank"):
            return t
    return open_tabs[0]


def _call(method: str, params: dict | None = None, timeout: float = 6.0) -> dict:
    """Send one DevTools command to the selected tab and wait for its answer. Caller holds _lock."""
    tab = _pick(tabs())
    if _conn["target"] != tab["id"] or _conn["ws"] is None:
        _drop()
        _conn["ws"] = connect(tab["ws"], max_size=None, open_timeout=3)
        _conn["target"] = tab["id"]
    _conn["id"] += 1
    msg_id = _conn["id"]
    try:
        _conn["ws"].send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            reply = json.loads(_conn["ws"].recv(timeout=timeout))
            if reply.get("id") == msg_id:
                if "error" in reply:
                    raise RuntimeError(reply["error"].get("message", "DevTools error"))
                return reply.get("result", {})
    except (TimeoutError, OSError, ConnectionError) as e:
        _drop()
        raise NoBrowser() from e
    except Exception as e:
        if "ConnectionClosed" in type(e).__name__:
            _drop()
            raise NoBrowser() from e
        raise


def status() -> dict:
    open_tabs = tabs()
    if not open_tabs:
        return {"open": False, "tabs": []}
    cur = _pick(open_tabs)
    return {"open": True, "current": cur["id"], "url": cur["url"], "title": cur["title"],
            "tabs": [{"id": t["id"], "title": t["title"] or t["url"], "url": t["url"]} for t in open_tabs]}


def select(tab_id: str) -> None:
    _conn["want"] = tab_id


def frame() -> bytes:
    """A JPEG of the current tab, sized to its window."""
    with _lock:
        try:
            m = _call("Page.getLayoutMetrics")
            vp = m.get("cssLayoutViewport") or {}
            _conn["size"] = (vp.get("clientWidth") or 1280, vp.get("clientHeight") or 900)
            shot = _call("Page.captureScreenshot", {"format": "jpeg", "quality": 70, "fromSurface": True})
        except NoBrowser:
            raise
        except Exception as e:
            _drop()
            raise NoBrowser() from e
    return base64.b64decode(shot["data"])


def _xy(fx: float, fy: float) -> tuple[float, float]:
    w, h = _conn["size"]
    return max(0.0, min(1.0, fx)) * w, max(0.0, min(1.0, fy)) * h


def click(fx: float, fy: float) -> None:
    with _lock:
        x, y = _xy(fx, fy)
        _call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        for kind in ("mousePressed", "mouseReleased"):
            _call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1})


def scroll(fx: float, fy: float, dy: float) -> None:
    with _lock:
        x, y = _xy(fx, fy)
        _call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0,
                                           "deltaY": max(-2000.0, min(2000.0, dy))})


def type_text(text: str) -> None:
    if text:
        with _lock:
            _call("Input.insertText", {"text": text[:2000]})


def key(name: str, shift: bool = False) -> None:
    if name not in _KEYS:
        raise ValueError("Unsupported key.")
    k, code, text = _KEYS[name]
    mods = 8 if shift else 0
    with _lock:
        down = {"type": "keyDown" if text else "rawKeyDown", "key": k, "code": k,
                "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code, "modifiers": mods}
        if text:
            down["text"] = text
        _call("Input.dispatchKeyEvent", down)
        _call("Input.dispatchKeyEvent", {"type": "keyUp", "key": k, "code": k, "windowsVirtualKeyCode": code,
                                         "nativeVirtualKeyCode": code, "modifiers": mods})


_EMAIL_STEP_JS = """(() => {
  if (!/\\/(access|login)/.test(location.pathname)) return 'past';
  const f = [...document.querySelectorAll('input[name=email], input[type=email], input[autocomplete=email]')].find(i => i.offsetParent);
  if (!f) return 'wait';
  if (f.value) return 'filled';
  f.focus();
  return 'ready';
})()"""

_CONTINUE_JS = """(() => {
  const b = [...document.querySelectorAll('button, input[type=submit]')]
    .find(x => x.offsetParent && /continue with email|^continue$|^next$/i.test((x.innerText || x.value || '').trim()));
  if (b) { b.click(); return true; }
  return false;
})()"""


def prefill_email(email: str, wait: float = 25.0) -> str:
    """On Handshake's sign-in page, type your Handshake email and click Continue. Only the email:
    the password or Google step is always yours. Returns what happened."""
    import time

    if not email:
        return "no-email"
    end = time.time() + wait
    while time.time() < end:
        try:
            with _lock:
                state = _call("Runtime.evaluate", {"expression": _EMAIL_STEP_JS, "returnByValue": True})["result"].get("value")
                if state == "ready":
                    _call("Input.insertText", {"text": email})
                    time.sleep(0.4)
                    _call("Runtime.evaluate", {"expression": _CONTINUE_JS, "returnByValue": True})
                    return "filled"
            if state in ("past", "filled"):
                return state
        except NoBrowser:
            pass
        time.sleep(0.7)
    return "timeout"


def go(action: str) -> None:
    with _lock:
        if action == "back":
            _call("Runtime.evaluate", {"expression": "history.back()"})
        elif action == "reload":
            _call("Page.reload")
        else:
            raise ValueError("Unsupported action.")
