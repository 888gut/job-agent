"""Read a resume PDF's real layout (fonts, sizes, positions) and mirror it in a .docx.

The .docx is only an intermediate: it is uploaded to Google Drive and converted into the
"master" Google Doc for that resume. The original PDF is never modified.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

import pymupdf
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

EDITABLE = ("bullet", "labeled", "text")
BULLETS = "•●▪◦\x7f·"
FONT = "Times New Roman"
PDF_FONTS = {"regular": "tiro", "bold": "tibo", "italic": "tiit"}  # built-in Times metrics


@dataclass
class Block:
    kind: str                  # name | contact | heading | role | sub | bullet | labeled | text
    text: str                  # full text, used for reading and for matching edits
    left: str = ""             # role/sub: left part; labeled: the bold label
    right: str = ""            # role/sub: right-aligned part (dates)
    lines: int = 1             # how many lines it occupies in the PDF
    space_before: float = 0.0  # points
    links: list = field(default_factory=list)  # contact: [(text, uri)]
    top: float = 0.0           # y of the block's first line on the page (for calibration)

    @property
    def editable(self) -> bool:
        return self.kind in EDITABLE


@dataclass
class Layout:
    blocks: list[Block]
    page_w: float
    margin_left: float
    margin_right: float
    margin_top: float
    body_x: float              # indent of body text, from page edge
    bullet_x: float            # bullet glyph
    bullet_text_x: float       # bullet text
    content_right: float = 0.0 # right edge of the text box (can be inside the heading rules)
    date_right: float = 0.0    # where right-aligned dates end
    page_h: float = 792.0
    pitch: float = 12.0        # line pitch of 10pt body text
    body_size: float = 10.0

    @property
    def text_width(self) -> float:
        return self.page_w - self.margin_left - self.margin_right

    @property
    def right_inset(self) -> float:
        """Gap between the end of body text and the right margin (heading rules run to the margin)."""
        return max(0.0, round(self.page_w - self.margin_right - self.content_right, 1))

    def width_for(self, kind: str) -> float:
        start = self.bullet_text_x if kind == "bullet" else self.body_x
        return self.content_right - start


# ---------------------------------------------------------------- parsing
def _style(span) -> str:
    f = span["font"].lower()
    if "bold" in f or span["flags"] & 16:
        return "bold"
    if "italic" in f or "oblique" in f or span["flags"] & 2:
        return "italic"
    return "regular"


def _rows(page) -> list[list[dict]]:
    """Group spans into visual rows by baseline."""
    spans = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                # Google Docs' PDF export adds zero-width characters around tabs; ignore them
                s["text"] = s["text"].replace("​", "").replace("﻿", "")
                if s["text"].strip():
                    spans.append(s)
    spans.sort(key=lambda s: (round(s["origin"][1]), s["bbox"][0]))
    rows: list[list[dict]] = []
    for s in spans:
        if rows and abs(rows[-1][0]["origin"][1] - s["origin"][1]) < 3:
            rows[-1].append(s)
        else:
            rows.append([s])
    for r in rows:
        r.sort(key=lambda s: s["bbox"][0])
    return rows


def _join(spans) -> str:
    out = ""
    for s in spans:
        t = s["text"]
        if out and not out.endswith(" ") and not t.startswith(" ") and s["bbox"][0] - prev_x1 > 1.5:
            out += " "
        out += t
        prev_x1 = s["bbox"][2]
    return re.sub(r"\s+", " ", out).strip()


def parse(pdf_path: str | Path) -> Layout:
    doc = pymupdf.open(str(pdf_path))
    page = doc[0]
    rows = []
    for pno in range(len(doc)):
        rows.extend(_rows(doc[pno]))
    page_w = page.rect.width
    rules = [d["rect"] for d in page.get_drawings() if d["rect"].height < 2 and d["rect"].width > page_w / 2]
    sizes = [round(s["size"], 1) for r in rows for s in r]
    body_size = median(sizes) if sizes else 10.0

    blocks: list[Block] = []
    tops: list[tuple[float, float]] = []    # (first row top, last row top) per block
    xs = {"body": [], "bullet": [], "bullet_text": [], "heading": [], "right": [], "line_end": []}
    cont_x = None                           # x where a wrapped continuation line starts

    for r in rows:
        first = r[0]
        top = min(s["bbox"][1] for s in r)
        st = _style(first)
        big = max(s["size"] for s in r)
        far = [s for s in r if s["bbox"][0] > page_w * 0.55 and s is not first]
        near = [s for s in r if s not in far]
        is_bullet = first["text"].strip() in BULLETS or first["text"].strip()[:1] in BULLETS
        if far:
            xs["right"].append(max(s["bbox"][2] for s in far))
        xs["line_end"].append(max(s["bbox"][2] for s in r))

        # wrapped continuation of the previous paragraph?
        if (blocks and cont_x is not None and st == "regular" and not is_bullet
                and abs(first["bbox"][0] - cont_x) < 2.5 and top - tops[-1][1] < body_size * 1.45):
            b = blocks[-1]
            nxt = _join(r)
            # a line that ends in a hyphenated word ("data-" / "informed") joins without a space
            b.text = f"{b.text}{nxt}" if re.search(r"[A-Za-z]-$", b.text) else f"{b.text} {nxt}"
            b.lines += 1
            tops[-1] = (tops[-1][0], top)
            continue

        cont_x = None
        if big >= body_size * 1.4 and st == "bold" and not blocks:
            blk = Block("name", _join(r))
        elif big < body_size * 0.95 and st == "regular":
            blk = Block("contact", _join(r))
        elif st == "bold" and _join(r).upper() == _join(r) and big > body_size:
            blk = Block("heading", _join(r))
            xs["heading"].append(first["bbox"][0])
        elif is_bullet:
            rest = r[1:] if first["text"].strip() in BULLETS else r
            text = _join(rest).lstrip(BULLETS).strip()
            blk = Block("bullet", text)
            xs["bullet"].append(first["bbox"][0])
            cont_x = rest[0]["bbox"][0] if rest else first["bbox"][2]
            xs["bullet_text"].append(cont_x)
        elif st == "bold" and far:
            blk = Block("role", f"{_join(near)} {_join(far)}", left=_join(near), right=_join(far))
            xs["body"].append(first["bbox"][0])
        elif st == "bold" and len(near) > 1 and _style(near[1]) == "regular":
            label = _join([s for s in near if _style(s) == "bold"])
            blk = Block("labeled", _join(near), left=label)
            xs["body"].append(first["bbox"][0])
            cont_x = first["bbox"][0]
        elif st == "bold":
            blk = Block("role", _join(near), left=_join(near))
            xs["body"].append(first["bbox"][0])
        elif st == "italic":
            blk = Block("sub", f"{_join(near)} {_join(far)}".strip(), left=_join(near), right=_join(far))
            xs["body"].append(first["bbox"][0])
        else:
            blk = Block("text", _join(r))
            xs["body"].append(first["bbox"][0])
            cont_x = first["bbox"][0]
        blk.top = round(top, 1)
        blocks.append(blk)
        tops.append((top, top))

    # contact links
    for b in blocks:
        if b.kind == "contact":
            for ln in page.get_links():
                if ln.get("uri"):
                    t = page.get_textbox(ln["from"]).strip()
                    if t and t in b.text:
                        b.links.append((t, ln["uri"]))

    # vertical rhythm: extra space before each block, relative to normal line pitch
    pitch = body_size * 1.2
    for i in range(1, len(blocks)):
        gap = tops[i][0] - tops[i - 1][1]
        blocks[i].space_before = max(0.0, round(gap - pitch, 1))

    def pick(vals, default):
        return round(median(vals), 1) if vals else default

    margin_left = pick(xs["heading"], pick(xs["body"], 54.0))
    margin_right = round(page_w - max(r.x1 for r in rules), 1) if rules else margin_left
    body_x = pick(xs["body"], margin_left)
    first_top = tops[0][0] if tops else 36.0
    # Text box right edge = the end of the longest line. Every original line fits in it and any
    # word that didn't fit originally still won't, so paragraphs wrap exactly as in the PDF.
    # Right-aligned dates get their own edge (they can sit slightly inside the text box).
    right_edge = page_w - margin_right
    content_right = round(max(xs["line_end"]), 1) if xs["line_end"] else right_edge
    content_right = min(content_right, right_edge)
    date_right = min(round(median(xs["right"]), 1), content_right) if xs["right"] else content_right
    return Layout(
        blocks=blocks, page_w=page_w, page_h=page.rect.height, margin_left=margin_left, margin_right=margin_right,
        margin_top=max(18.0, round(first_top - 4, 1)), body_x=body_x,
        bullet_x=pick(xs["bullet"], body_x + 6), bullet_text_x=pick(xs["bullet_text"], body_x + 16),
        content_right=content_right, date_right=date_right, pitch=pitch, body_size=body_size,
    )


def calibrate(target: Layout, rendered: Layout) -> bool:
    """Nudge target's spacing so a rebuilt master lands where the original PDF's lines are.

    `rendered` is the parse of the master as Google Docs actually rendered it. Adjusts the top
    margin and each block's space-before by the measured difference. Returns True if anything
    moved by more than half a point (i.e. another build is worthwhile).
    """
    if len(target.blocks) != len(rendered.blocks):
        return False
    changed = False
    d0 = target.blocks[0].top - rendered.blocks[0].top
    if abs(d0) > 0.5:
        target.margin_top = max(10.0, round(target.margin_top + d0, 1))
        changed = True
    for i in range(1, len(target.blocks)):
        want = target.blocks[i].top - target.blocks[i - 1].top
        got = rendered.blocks[i].top - rendered.blocks[i - 1].top
        delta = want - got
        if abs(delta) > 0.5:
            b = target.blocks[i]
            new = max(0.0, round(b.space_before + delta, 1))
            if new != b.space_before:
                b.space_before = new
                changed = True
    return changed


# ---------------------------------------------------------------- fit check
def _word_width(word: str, style: str, size: float) -> float:
    return pymupdf.get_text_length(word, fontname=PDF_FONTS[style], fontsize=size)


def lines_needed(text: str, width: float, size: float = 10.0, bold_prefix: str = "") -> int:
    space = _word_width(" ", "regular", size)
    lines, x = 1, 0.0
    bold_words = len(bold_prefix.split())
    for i, w in enumerate(text.split()):
        ww = _word_width(w, "bold" if i < bold_words else "regular", size)
        add = ww if x == 0 else space + ww
        if x + add > width and x > 0:
            lines += 1
            x = ww
        else:
            x += add
    return lines


FIT_SLACK = 1.02


def fits(layout: Layout, block: Block, new_text: str) -> tuple[bool, int, int]:
    """Does new_text fit in the same number of lines as the original block?"""
    width = layout.width_for(block.kind)
    prefix = block.left if block.kind == "labeled" else ""
    old_n = lines_needed(block.text, width, layout.body_size, prefix)
    # 2% slack: the measured widths are a little conservative against the real PDF rendering
    new_n = lines_needed(new_text, width * FIT_SLACK, layout.body_size, prefix)
    return new_n <= max(old_n, block.lines), old_n, new_n


BOTTOM_MARGIN = 36.0  # the masters use a 0.5in bottom margin


def section_of(layout: Layout) -> list[str]:
    """Heading text each block sits under ("" before the first heading)."""
    out, current = [], ""
    for b in layout.blocks:
        if b.kind == "heading":
            current = b.text.upper()
        out.append(current)
    return out


def project_slot(layout: Layout) -> int | None:
    """Index of the last block in the PROJECTS section (a new project goes right after it)."""
    secs = section_of(layout)
    idx = [i for i, s in enumerate(secs) if "PROJECT" in s and layout.blocks[i].kind != "heading"]
    return idx[-1] if idx else None


def project_room(layout: Layout, title: str, bullet: str) -> tuple[bool, str]:
    """Can a project entry (bold title line + one bullet) be added and stay on one page?"""
    last = project_slot(layout)
    if last is None:
        return False, "this resume has no Projects section"
    if lines_needed(bullet, layout.width_for("bullet"), layout.body_size) > 1:
        return False, "the project bullet is longer than one line"
    if lines_needed(title, layout.content_right - layout.body_x, layout.body_size, title) > 1:
        return False, "the project title is longer than one line"
    secs = section_of(layout)
    proj = [b for i, b in enumerate(layout.blocks) if "PROJECT" in secs[i]]
    role_sb = next((b.space_before for b in proj if b.kind == "role"), 12.0)
    bullet_sb = next((b.space_before for b in proj if b.kind == "bullet"), 5.0)
    need = role_sb + layout.pitch + bullet_sb + layout.pitch
    end = layout.blocks[-1]
    used = end.top + end.lines * layout.pitch
    free = layout.page_h - BOTTOM_MARGIN - used
    if need > free:
        return False, f"not enough room on the page ({need:.0f}pt needed, {free:.0f}pt free)"
    return True, ""


# ---------------------------------------------------------------- docx builder
SINGLE_FACTOR = 1.15  # Google Docs' single line height for Times New Roman = 1.15 x font size


def _spacing(p, multiple: float) -> None:
    """Line spacing as a multiple of single. (Google Docs drops 'exactly N pt' spacing on import,
    but keeps a multiple, so this is how we hit the original's exact line pitch.)"""
    pf = p.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf.line_spacing = round(multiple, 3)


def _run(p, text: str, size: float, bold=False, italic=False, small_caps=False, font=FONT):
    r = p.add_run(text)
    r.font.name = font
    r._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), font)
    r.font.size = Pt(size)
    r.font.bold, r.font.italic = bold, italic
    if small_caps:
        r.font.small_caps = True
    r.font.color.rgb = RGBColor(0, 0, 0)
    return r


def _bottom_border(p) -> None:
    pPr = p._p.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for k, v in (("w:val", "single"), ("w:sz", "6"), ("w:space", "1"), ("w:color", "000000")):
        bottom.set(qn(k), v)
    bdr.append(bottom)
    pPr.append(bdr)


def _hyperlink(p, text: str, uri: str, size: float) -> None:
    rid = p.part.relate_to(uri, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
                           is_external=True)
    h = OxmlElement("w:hyperlink")
    h.set(qn("r:id"), rid)
    r = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    fonts = OxmlElement("w:rFonts")
    for a in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        fonts.set(qn(a), FONT)
    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), str(int(size * 2)))
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "000000")
    rpr.extend([fonts, color, sz])
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    r.extend([rpr, t])
    h.append(r)
    p._p.append(h)


def _title_case(s: str) -> str:
    small = {"and", "of", "in", "the", "for", "to", "a", "an", "&"}
    return " ".join(w.lower() if w.lower() in small and i else w.capitalize() for i, w in enumerate(s.split()))


def build_docx(layout: Layout, out_path: str | Path) -> Path:
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Pt(layout.page_w), Pt(792)
    sec.left_margin, sec.right_margin = Pt(layout.margin_left), Pt(layout.margin_right)
    sec.top_margin, sec.bottom_margin = Pt(layout.margin_top), Pt(36)

    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = FONT, Pt(layout.body_size)
    normal.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), FONT)
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.space_before = Pt(0)

    size = layout.body_size
    indent = Pt(layout.body_x - layout.margin_left)
    right_tab = Pt((layout.date_right or layout.content_right) - layout.margin_left)
    right_indent = Pt(layout.right_inset)
    body_multiple = layout.pitch / (size * SINGLE_FACTOR)

    for b in layout.blocks:
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.space_before = Pt(b.space_before)
        _spacing(p, body_multiple)
        if b.kind not in ("name", "contact", "heading"):
            pf.right_indent = right_indent
        if b.kind == "name":
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _spacing(p, 1.0)
            _run(p, b.text, size * 1.6, bold=True)
        elif b.kind == "contact":
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            text = b.text
            for link_text, uri in b.links:
                before, _, text = text.partition(link_text)
                if before:
                    _run(p, before, size * 0.9)
                _hyperlink(p, link_text, uri, size * 0.9)
            if text:
                _run(p, text, size * 0.9)
        elif b.kind == "heading":
            _spacing(p, 1.0)
            _run(p, _title_case(b.text), size * 1.1, bold=True, small_caps=True)
            _bottom_border(p)
        elif b.kind in ("role", "sub"):
            pf.left_indent = indent
            pf.tab_stops.add_tab_stop(right_tab, WD_TAB_ALIGNMENT.RIGHT)
            _run(p, b.left, size, bold=b.kind == "role", italic=b.kind == "sub")
            if b.right:
                _run(p, "\t" + b.right, size, italic=b.kind == "sub")
        elif b.kind == "bullet":
            hang = Pt(layout.bullet_text_x - layout.bullet_x)
            pf.left_indent = Pt(layout.bullet_text_x - layout.margin_left)
            pf.first_line_indent = -hang
            pf.tab_stops.add_tab_stop(Pt(layout.bullet_text_x - layout.margin_left))
            _run(p, "•", size, font="Arial")
            _run(p, "\t" + b.text, size)
        elif b.kind == "labeled":
            pf.left_indent = indent
            _run(p, b.left, size, bold=True)
            _run(p, b.text[len(b.left):], size)
        else:
            pf.left_indent = indent
            _run(p, b.text, size)

    # python-docx starts with one empty paragraph; remove it
    first = doc.paragraphs[0]
    if not first.text and len(doc.paragraphs) > 1:
        first._p.getparent().remove(first._p)
    doc.save(str(out_path))
    return Path(out_path)


def render_png(pdf_path: str | Path, out_png: str | Path, zoom: float = 1.4) -> Path:
    page = pymupdf.open(str(pdf_path))[0]
    page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).save(str(out_png))
    return Path(out_png)
