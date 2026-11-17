"""Google OAuth for the two Gmail accounts, plus Sheets/Drive storage for the storage account."""
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .config import GOOGLE_CLIENT_FILE, TOKENS
from .db import get_settings, save_settings

STORAGE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]
ACCOUNTS = {"storage": STORAGE_SCOPES, "handshake_gmail": GMAIL_SCOPES}

SHEET_TITLE = "Job Agent - Applications"
SHEET_HEADER = [
    "Applied (date)", "Company", "Role", "Location", "Handshake link",
    "Resume used", "Tailored", "Branch", "Match score", "Status", "Job ID", "Resume file",
]
STATUS_COL = "J"  # column letter of "Status"
DOC_MIME = "application/vnd.google-apps.document"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class NotConnected(RuntimeError):
    pass


def _token_path(account: str) -> Path:
    return TOKENS / f"{account}.json"


def credentials(account: str, interactive: bool = False) -> Credentials:
    scopes = ACCOUNTS[account]
    path = _token_path(account)
    creds: Optional[Credentials] = None
    if path.exists():
        creds = Credentials.from_authorized_user_file(str(path), scopes)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            path.write_text(creds.to_json())
            return creds
        except Exception:
            creds = None
    if not interactive:
        raise NotConnected(f"Google account '{account}' is not connected.")
    if not GOOGLE_CLIENT_FILE.exists():
        raise NotConnected(f"Missing Google OAuth client file: {GOOGLE_CLIENT_FILE}")
    flow = InstalledAppFlow.from_client_secrets_file(str(GOOGLE_CLIENT_FILE), scopes)
    # Opens the user's browser on Google's own consent page; they pick the account.
    creds = flow.run_local_server(port=0, prompt="select_account consent", open_browser=True)
    path.write_text(creds.to_json())
    return creds


def account_email(account: str) -> Optional[str]:
    try:
        creds = credentials(account)
        info = build("oauth2", "v2", credentials=creds, cache_discovery=False).userinfo().get().execute()
        return info.get("email")
    except Exception:
        return None


def disconnect(account: str) -> None:
    _token_path(account).unlink(missing_ok=True)


def is_connected(account: str) -> bool:
    try:
        credentials(account)
        return True
    except Exception:
        return False


# ---------------- Sheets / Drive (storage account) ----------------
def _sheets():
    return build("sheets", "v4", credentials=credentials("storage"), cache_discovery=False)


def _drive():
    return build("drive", "v3", credentials=credentials("storage"), cache_discovery=False)


def ensure_sheet() -> str:
    s = get_settings()
    if s.get("sheet_id"):
        return s["sheet_id"]
    sheet = _sheets().spreadsheets().create(
        body={"properties": {"title": SHEET_TITLE}, "sheets": [{"properties": {"title": "Applications"}}]},
        fields="spreadsheetId",
    ).execute()
    sid = sheet["spreadsheetId"]
    _sheets().spreadsheets().values().update(
        spreadsheetId=sid, range="Applications!A1",
        valueInputOption="RAW", body={"values": [SHEET_HEADER]},
    ).execute()
    save_settings({"sheet_id": sid})
    return sid


def sheet_url() -> Optional[str]:
    sid = get_settings().get("sheet_id")
    return f"https://docs.google.com/spreadsheets/d/{sid}" if sid else None


def ensure_drive_folder() -> str:
    s = get_settings()
    if s.get("drive_folder_id"):
        return s["drive_folder_id"]
    folder = _drive().files().create(
        body={"name": "Job Agent Resumes", "mimeType": "application/vnd.google-apps.folder"},
        fields="id",
    ).execute()
    save_settings({"drive_folder_id": folder["id"]})
    return folder["id"]


def doc_url(doc_id: str) -> str:
    return f"https://docs.google.com/document/d/{doc_id}/edit"


# ---------------- Google Docs: masters and tailored copies ----------------
def upload_as_doc(docx_path: Path, name: str) -> str:
    """Upload a .docx and let Drive convert it into a Google Doc. Returns the Doc id."""
    f = _drive().files().create(
        body={"name": name, "mimeType": DOC_MIME, "parents": [ensure_drive_folder()]},
        media_body=MediaFileUpload(str(docx_path), mimetype=DOCX_MIME),
        fields="id",
    ).execute()
    return f["id"]


def trash_file(file_id: str) -> None:
    """Move a file the agent created to Drive's trash (recoverable for 30 days)."""
    _drive().files().update(fileId=file_id, body={"trashed": True}).execute()


def copy_doc(doc_id: str, name: str) -> str:
    f = _drive().files().copy(
        fileId=doc_id, body={"name": name, "parents": [ensure_drive_folder()]}, fields="id"
    ).execute()
    return f["id"]


def export_pdf(doc_id: str, out_path: Path) -> Path:
    data = _drive().files().export(fileId=doc_id, mimeType="application/pdf").execute()
    out_path.write_bytes(data)
    return out_path


def apply_doc_edits(doc_id: str, edits: list[dict]) -> list[str]:
    """Swap edited text into a Doc, keeping each line's original styling. Returns edits not found."""
    docs = build("docs", "v1", credentials=credentials("storage"), cache_discovery=False)
    document = docs.documents().get(documentId=doc_id).execute()
    requests, missing = edit_requests(document, edits)
    if requests:
        docs.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()
    return missing


_STYLE_KEYS = ("bold", "italic", "underline", "smallCaps", "fontSize", "weightedFontFamily",
               "foregroundColor", "baselineOffset")


def _u16(s: str) -> int:
    """Google Docs indexes count UTF-16 code units."""
    return len(s.encode("utf-16-le")) // 2


def _common_prefix(a: str, b: str) -> str:
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    if n < len(a) or n < len(b):  # back off to a word boundary
        cut = a.rfind(" ", 0, n + 1)
        n = cut + 1 if cut >= 0 else 0
    return a[:n]


def _common_suffix(a: str, b: str) -> str:
    n = 0
    while n < min(len(a), len(b)) and a[-1 - n] == b[-1 - n]:
        n += 1
    if n < len(a) or n < len(b):
        tail = a[len(a) - n:]
        cut = tail.find(" ")
        n = n - cut if cut >= 0 else 0
    return a[len(a) - n:] if n else ""


def edit_requests(document: dict, edits: list[dict]) -> tuple[list[dict], list[str]]:
    """Build Docs API requests that replace each edit's old text with its new text.

    Only the part that actually changed is replaced, and the inserted text is given the exact
    style of the text it replaces, so bold labels, fonts and sizes stay as they were.
    """
    paras = []  # (paragraph text, [(startIndex, endIndex, textStyle)])
    for el in document.get("body", {}).get("content", []):
        p = el.get("paragraph")
        if not p:
            continue
        text, runs = "", []
        for e in p.get("elements", []):
            tr = e.get("textRun")
            if tr:
                runs.append((e["startIndex"], e["endIndex"], tr.get("textStyle", {})))
                text += tr["content"]
        if runs:
            paras.append((text, runs))

    ops, missing = [], []
    for ed in edits:
        old, new = ed["old"], ed["new"]
        hits = [(t, r) for t, r in paras if old in t]
        if len(hits) != 1:
            missing.append(old)
            continue
        text, runs = hits[0]
        base = runs[0][0] + _u16(text[: text.index(old)])
        pre = _common_prefix(old, new)
        suf = _common_suffix(old[len(pre):], new[len(pre):])
        start = base + _u16(pre)
        end = base + _u16(old[: len(old) - len(suf)])
        insert = new[len(pre): len(new) - len(suf)]
        style = {}
        for s, e, st in runs:
            if s <= start < e or (start == end and s < start <= e):
                style = st
                break
        ops.append((start, end, insert, style))

    requests = []
    for start, end, insert, style in sorted(ops, key=lambda o: o[0], reverse=True):
        if end > start:
            requests.append({"deleteContentRange": {"range": {"startIndex": start, "endIndex": end}}})
        if insert:
            requests.append({"insertText": {"location": {"index": start}, "text": insert}})
            keep = {k: v for k, v in style.items() if k in _STYLE_KEYS}
            fields = ",".join(_STYLE_KEYS)
            requests.append({"updateTextStyle": {
                "range": {"startIndex": start, "endIndex": start + _u16(insert)},
                "textStyle": keep, "fields": fields}})
    return requests, missing


_PARA_KEYS = ("indentStart", "indentEnd", "indentFirstLine", "spaceAbove", "spaceBelow", "lineSpacing",
              "alignment", "direction", "spacingMode")


def _paragraphs(document: dict) -> list[dict]:
    out = []
    for el in document.get("body", {}).get("content", []):
        p = el.get("paragraph")
        if not p:
            continue
        runs = [(e["startIndex"], e["endIndex"], e["textRun"].get("textStyle", {}), e["textRun"]["content"])
                for e in p.get("elements", []) if "textRun" in e]
        if runs:
            out.append({"text": "".join(r[3] for r in runs), "runs": runs,
                        "start": el["startIndex"], "end": el["endIndex"], "style": p.get("paragraphStyle", {})})
    return out


def _find(paras: list[dict], text: str) -> dict | None:
    hits = [p for p in paras if text and text in p["text"]]
    return hits[0] if len(hits) == 1 else None


def project_requests(document: dict, after_text: str, title_sample: str, bullet_sample: str,
                     title: str, bullet: str) -> list[dict]:
    """Insert a project entry (bold title line + one bullet) right after the paragraph containing
    `after_text`, copying paragraph and text styles from an existing project title and bullet."""
    paras = _paragraphs(document)
    after, t_sample, b_sample = _find(paras, after_text), _find(paras, title_sample), _find(paras, bullet_sample)
    if not (after and t_sample and b_sample):
        raise ValueError("couldn't find where to add the project in the Google Doc")
    at = after["end"] - 1                       # just before the paragraph's newline
    bullet_line = "•\t" + bullet
    t_start = at + 1
    b_start = t_start + _u16(title) + 1
    reqs = [{"insertText": {"location": {"index": at}, "text": f"\n{title}\n{bullet_line}"}}]

    def para_style(rng_start, rng_end, sample):
        st = {k: v for k, v in sample["style"].items() if k in _PARA_KEYS}
        if st:
            reqs.append({"updateParagraphStyle": {"range": {"startIndex": rng_start, "endIndex": rng_end},
                                                  "paragraphStyle": st, "fields": ",".join(st)}})

    def text_style(rng_start, rng_end, style):
        keep = {k: v for k, v in style.items() if k in _STYLE_KEYS}
        reqs.append({"updateTextStyle": {"range": {"startIndex": rng_start, "endIndex": rng_end},
                                         "textStyle": keep, "fields": ",".join(_STYLE_KEYS)}})

    para_style(t_start, t_start + _u16(title), t_sample)
    para_style(b_start, b_start + _u16(bullet_line), b_sample)
    text_style(t_start, t_start + _u16(title), t_sample["runs"][0][2])
    glyph_style = b_sample["runs"][0][2]
    body_style = next((r[2] for r in b_sample["runs"] if r[3].strip("•\t\n ")), glyph_style)
    text_style(b_start, b_start + 1, glyph_style)
    text_style(b_start + 1, b_start + _u16(bullet_line), body_style)
    return reqs


def add_project(doc_id: str, after_text: str, title_sample: str, bullet_sample: str,
                title: str, bullet: str) -> None:
    docs = build("docs", "v1", credentials=credentials("storage"), cache_discovery=False)
    document = docs.documents().get(documentId=doc_id).execute()
    reqs = project_requests(document, after_text, title_sample, bullet_sample, title, bullet)
    docs.documents().batchUpdate(documentId=doc_id, body={"requests": reqs}).execute()


# ---------------- Sheet ----------------
def sheet_row(job, app, resume_link: str = "", branch: str = "") -> list:
    return [
        app.applied_at.astimezone().strftime("%Y-%m-%d %H:%M"),   # your local time
        job.company,
        job.title,
        job.location,
        job.url,
        f"Resume {app.resume_slot}",
        "Yes" if app.tailored else "No",
        branch,
        app.score if app.score is not None else "",
        app.status,
        job.handshake_id,
        resume_link,
    ]


def append_application(job, app, resume_link: str = "", branch: str = "") -> Optional[int]:
    sid = ensure_sheet()
    res = _sheets().spreadsheets().values().append(
        spreadsheetId=sid, range="Applications!A1", valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS", body={"values": [sheet_row(job, app, resume_link, branch)]},
    ).execute()
    updated = res.get("updates", {}).get("updatedRange", "")  # e.g. Applications!A7:L7
    if "!" in updated:
        digits = "".join(c for c in updated.split("!")[1].split(":")[0] if c.isdigit())
        return int(digits) if digits else None
    return None


def update_status(sheet_row_num: int, status: str) -> None:
    sid = ensure_sheet()
    _sheets().spreadsheets().values().update(
        spreadsheetId=sid, range=f"Applications!{STATUS_COL}{sheet_row_num}",
        valueInputOption="RAW", body={"values": [[status]]},
    ).execute()
