"""Reading resumes (.docx / .pdf), numbering paragraphs, and PDF export."""
import re
from pathlib import Path
from typing import Iterator

from docx import Document
from docx.text.paragraph import Paragraph


def iter_paragraphs(doc) -> Iterator[Paragraph]:
    """Body paragraphs plus paragraphs inside tables, in document order.

    The same iterator is used for reading and for applying edits, so indexes line up.
    """
    for block in doc.element.body.iterchildren():
        tag = block.tag.rsplit("}", 1)[-1]
        if tag == "p":
            yield Paragraph(block, doc)
        elif tag == "tbl":
            for row in block.iter():
                if row.tag.rsplit("}", 1)[-1] == "p":
                    yield Paragraph(row, doc)


def paragraphs(path: str | Path) -> list[str]:
    path = Path(path)
    if path.suffix.lower() == ".docx":
        return [p.text for p in iter_paragraphs(Document(str(path)))]
    if path.suffix.lower() == ".pdf":
        # Read-only, from the PDF's real layout: whole bullets/paragraphs. The PDF itself is never changed.
        from .layout import parse

        try:
            return [b.text for b in parse(path).blocks]
        except Exception:
            return [text for _, text in pdf_blocks(path)]
    raise ValueError(f"Unsupported resume type: {path.suffix}")


def _pdf_lines(path: Path) -> list[str]:
    from pypdf import PdfReader

    lines: list[str] = []
    for page in PdfReader(str(path)).pages:
        lines.extend((page.extract_text() or "").splitlines())
    return lines


def plain_text(path: str | Path) -> str:
    return "\n".join(p for p in paragraphs(path) if p.strip())


def numbered_text(paras: list[str]) -> str:
    return "\n".join(f"[{i}] {t}" for i, t in enumerate(paras) if t.strip())


BULLET_CHARS = "\x7f•●▪◦‣⁃-*"
_DATE_LINE = re.compile(
    r"^(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+)?(?:19|20)\d{2}"
    r"(?:\s*[–—-]\s*(?:present|current|(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+)?(?:19|20)\d{2}))?$",
    re.I,
)
WRAP_WIDTH = 95  # a PDF line at least this long most likely wrapped onto the next line


def _is_heading(line: str) -> bool:
    letters = [c for c in line if c.isalpha()]
    return bool(letters) and len(line) <= 40 and all(c.isupper() for c in letters)


def pdf_blocks(pdf_path: Path) -> list[tuple[str, str]]:
    """Turn raw PDF lines into (kind, text) blocks, rejoining lines the PDF wrapped.

    kind: name | contact | heading | role | date | bullet | text
    Used only to READ the PDF; nothing here writes or reformats a file.
    """
    raw = [l.rstrip() for l in _pdf_lines(Path(pdf_path)) if l.strip()]
    merged: list[tuple[str, str]] = []  # (kind, text) before role detection
    prev_len = 0
    for idx, line in enumerate(raw):
        stripped = line.strip()
        if not stripped:
            prev_len = 0
            continue
        is_bullet = stripped[0] in BULLET_CHARS
        text = stripped.lstrip(BULLET_CHARS).strip() if is_bullet else stripped
        if _is_heading(text):
            kind = "heading"
        elif _DATE_LINE.match(text):
            kind = "date"
        elif is_bullet:
            kind = "bullet"
        else:
            kind = "text"
        next_is_date = idx + 1 < len(raw) and bool(_DATE_LINE.match(raw[idx + 1].strip().lstrip(BULLET_CHARS).strip()))
        continues = (
            merged and kind == "text" and prev_len >= WRAP_WIDTH and merged[-1][0] in ("text", "bullet")
            and not merged[-1][1].endswith((".", "!", "?"))  # a finished sentence doesn't wrap on
            and not next_is_date                              # a line right above a date is a job title
        )
        if continues:
            k, t = merged[-1]
            merged[-1] = (k, f"{t} {text}")
        else:
            merged.append((kind, text))
        prev_len = len(line)

    out: list[tuple[str, str]] = []
    for i, (kind, text) in enumerate(merged):
        if i == 0:
            kind = "name"
        elif i == 1 and ("@" in text or "|" in text):
            kind = "contact"
        elif kind == "text" and i + 1 < len(merged) and merged[i + 1][0] == "date":
            kind = "role"
        out.append((kind, text))
    return out
