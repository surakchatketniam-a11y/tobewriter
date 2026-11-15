"""
Pocket-book interior builder (130 x 185 mm) for Tobe-Writer.

Independent of tools/build_book_pdf.py. Pipeline:
  1. Parse book.json, the episode Markdown, the front matter and the bibliography.
  2. Lay every block out once in a "galley" (one long column at the real text width)
     inside Chrome and measure its height in baseline-grid lines, plus the text offset
     where every line of every paragraph starts.
  3. Paginate in Python on the line grid: widows/orphans >= 2 lines, headings kept with
     at least two lines of text, full-page figures floated to the next page, chapters
     opening on a recto, front matter folioed with Thai letters.
  4. Write one HTML page per book page and print it with Chrome:
       *_proof.pdf  trim size, for reading on screen
       *_print.pdf  3 mm bleed + crop marks, TrimBox/BleedBox set for the printer
  5. Re-measure the final pages and report any page whose text overflows.

Usage:
  python tools/pocketbook/build_pocketbook.py --book 01_กระดานศึกพระเจ้าตาก --part 1
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import tempfile
from pathlib import Path

import markdown
import pymupdf
from bs4 import BeautifulSoup, NavigableString, Tag, Comment
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
FONTS = ("https://fonts.googleapis.com/css2?"
         "family=Noto+Serif+Thai:wght@400;500;600;700;800"
         "&family=Noto+Sans+Thai:wght@400;500;600;700&display=block")
# Google Fonts serves variable fonts to modern browsers and Chrome embeds those in PDF as
# Type 3 (printers reject them). With a legacy user agent it serves static per-weight
# files, which Chrome embeds as proper TrueType subsets.
STATIC_FONT_UA = "Mozilla/5.0 (Windows NT 6.1; Trident/7.0; rv:11.0) like Gecko"

# geometry (must agree with pocketbook.css)
LH_PT = 17.0
LINES = 25
PX_PER_PT = 96 / 72
LH_PX = LH_PT * PX_PER_PT
OPENER_FIRST_LINE = 11       # chapter title starts on grid line 11 (art covers lines 1-10)
SECTION_FIRST_LINE = 4       # section titles (preface etc.) start on line 4
TRIM_W, TRIM_H, BLEED, SLUG = 130.0, 185.0, 3.0, 10.0
ORPHANS = WIDOWS = 2

THAI_DIGITS = str.maketrans("0123456789", "๐๑๒๓๔๕๖๗๘๙")
THAI_LETTERS = list("กขคงจฉชซฌญฎฏฐฑฒณดตถทธนบปผฝพฟภมยรลวศษสหฬอฮ")

# Proper names the Thai dictionary breaker does not know; a line may never break inside them.
# Extend with tools/pocketbook/names.txt (one name per line).
NAMES = """
พันจันทนุมาศ วชิรปราการ มังระ เนเมียวสีหบดี มังมหานรธา สุกี้พระนายกอง โพสาวหาญ พรานนก หันตรา กบแจะ เจ้าโล้
บางปะกง ปราจีนบุรี นครนายก จันทบูร จันทบุรี สุริยาสน์อมรินทร์ สรรเพชญปราสาท ศรีสรรเพชญดาญาณ วิไชยประสิทธิ์
พิชัยสงคราม ชำนาญไพรสณฑ์ เชียงเงิน พิชัยอาสา พรหมเสนา ราชเสน่หา อภัยภักดี ทองอยู่นกเล็ก ดำรงราชานุภาพ
เอียวศรีวงศ์ คองบอง เฉียนหลง หมิงรุ่ย ลามาร์ บางปลาสร้อย ลุ่มมหาชัยชุมพล คีรีบัญชร สังข์กระจาย ใหญ่ชัยมงคล
โพธิ์สามต้น ข้าวเม่า ขื่อหน้า สีกุก ภูเขาทอง ขนอนหลวง โปรดสัตว์ เอกทัศ บรมโกศ เทพพิพิธ สวางคบุรี
นครศรีธรรมราช ธนบุรีศรีมหาสมุทร ปราบดาภิเษก ราชมณเฑียร จุลศักราช หมื่นซ่อง บุญเมือง แหลมปู่เจ้า นาจอมเทียน
ทุ่งเค็ด จีนเจียม ทองอิน แมงกี้มารหญ้า ไทรโยค สมุทรสงคราม ศิลาแลง สระบาป อังวะ หัตถเลขา นรินทรเทวี
ประดู่ทรงธรรม มหาราชวงษ์ พงษาวดาร ชุตินธรานนท์ บริติชมิวเซียม ทิพากรวงศ์ สิทธิพันธ์ ศุกลรัตนเมธี
พระยาตาก เจ้าตาก พระเจ้าตากสิน กรุงธนบุรี
""".split()


def load_names() -> list[str]:
    extra = HERE / "names.txt"
    names = list(NAMES)
    if extra.exists():
        names += [l.strip() for l in extra.read_text(encoding="utf-8").splitlines() if l.strip()]
    return sorted(set(names), key=len, reverse=True)


NAME_RE = re.compile("(" + "|".join(map(re.escape, load_names())) + ")")


# --------------------------------------------------------------------------- text
class Typo:
    def __init__(self, thai_digits: bool):
        self.thai_digits = thai_digits

    def digits(self, s: str) -> str:
        return s.translate(THAI_DIGITS) if self.thai_digits else s

    def num(self, n: int) -> str:
        return self.digits(str(n))

    def fix_soup(self, root: Tag) -> None:
        """Thai digits + curly quotes on every text node, quotes paired per block."""
        state = {"open": True}

        def walk(node):
            for ch in list(node.children):
                if isinstance(ch, Comment):
                    continue
                if isinstance(ch, NavigableString):
                    s = self.digits(str(ch))
                    out = []
                    for c in s:
                        if c == '"':
                            out.append("“" if state["open"] else "”")
                            state["open"] = not state["open"]
                        else:
                            out.append(c)
                    s = "".join(out)
                    # keep era abbreviations with their year
                    s = re.sub(r"(พ\.ศ\.|จ\.ศ\.|ค\.ศ\.) ", "\\1\u00a0", s)
                    pieces = NAME_RE.split(s)
                    if len(pieces) == 1 or (isinstance(node, Tag) and "nw" in (node.get("class") or [])):
                        ch.replace_with(NavigableString(s))
                        continue
                    new = []
                    for k, piece in enumerate(pieces):
                        if not piece:
                            continue
                        if k % 2:
                            sp = Tag(name="span", attrs={"class": "nw"})
                            sp.string = piece
                            new.append(sp)
                        else:
                            new.append(NavigableString(piece))
                    ch.replace_with(*new)
                elif isinstance(ch, Tag):
                    walk(ch)
        walk(root)

    def text(self, s: str) -> str:
        soup = BeautifulSoup(html.escape(s, quote=False), "html.parser")
        self.fix_soup(soup)
        return str(soup)


def inner_html(tag: Tag) -> str:
    return "".join(str(c) for c in tag.contents).strip()


def slice_html(src: str, start: int, end: int | None) -> str:
    """Return the part of an inline-HTML string between two textContent offsets."""
    soup = BeautifulSoup(src, "html.parser")
    pos = 0
    hi_lim = float("inf") if end is None else end

    def rec(node) -> str:
        nonlocal pos
        out = []
        for ch in node.children:
            if isinstance(ch, Comment):
                continue
            if isinstance(ch, NavigableString):
                s = str(ch)
                a, b = pos, pos + len(s)
                pos = b
                lo, hi = max(a, start), min(b, hi_lim)
                if lo < hi:
                    out.append(html.escape(s[lo - a:hi - a], quote=False))
            elif isinstance(ch, Tag):
                inner = rec(ch)
                if inner:
                    attrs = "".join(f' {k}="{html.escape(" ".join(v) if isinstance(v, list) else v)}"'
                                    for k, v in ch.attrs.items())
                    out.append(f"<{ch.name}{attrs}>{inner}</{ch.name}>")
        return "".join(out)

    return rec(soup)


# --------------------------------------------------------------------------- content model
class Block:
    _seq = 0

    def __init__(self, kind: str, html_: str = "", split: bool = False, **kw):
        Block._seq += 1
        self.id = f"b{Block._seq}"
        self.kind = kind          # p li h_act h_sub scene end fig bib bibtopic tl title
        self.html = html_
        self.split = split
        self.space = kw.pop("space", 0)       # blank grid lines before the block
        self.keep = kw.pop("keep", False)     # keep with next
        self.cls = kw.pop("cls", "")
        self.extra = kw
        self.lines = 0
        self.starts: list[int] = [0]

    # galley markup ---------------------------------------------------------
    def galley(self) -> str:
        return self.render(0, None, first=True, last=True, mid=True)

    def render(self, a: int, b: int | None, first: bool, last: bool, mid=False, space=0) -> str:
        """a/b are line indices for splittable blocks."""
        style = f' style="margin-top:{space * LH_PT}pt"' if space else ""
        midattr = f' data-mid="{self.id}" data-split="{int(self.split)}"' if mid else ""
        if self.split:
            lo = self.starts[a] if a < len(self.starts) else 0
            hi = self.starts[b] if (b is not None and b < len(self.starts)) else None
            body = slice_html(self.html, lo, hi) if not mid else self.html
        else:
            body = self.html
        cls = ["b", self.kind.replace("_", "-") if self.kind in ("h_act", "h_sub") else self.kind]
        if self.cls:
            cls.append(self.cls)
        if self.split and not mid:
            if not first:
                cls.append("cont")
            if not last:
                cls.append("split-a")
        tag = "p" if self.kind in ("p", "bib") else "div"
        if self.kind == "li":
            mk = self.extra.get("marker")
            mkh = f'<span class="mk">{mk}</span>' if (mk and first) else ""
            return f'<div class="{" ".join(cls)}"{midattr}{style}>{mkh}<span class="tx">{body}</span></div>'
        if self.split:
            return f'<{tag} class="{" ".join(cls)}"{midattr}{style}><span class="tx">{body}</span></{tag}>'
        return f'<{tag} class="{" ".join(cls)}"{midattr}{style}>{body}</{tag}>'


class Flow:
    """A run of blocks paginated together (a chapter body, the preface...)."""

    def __init__(self, key: str, blocks: list[Block], title: Block | None, opener: str,
                 rh_recto: str, rh_verso: str, art: str | None = None):
        self.key = key
        self.blocks = blocks
        self.title = title
        self.opener = opener      # "chapter" | "section"
        self.rh_recto = rh_recto
        self.rh_verso = rh_verso
        self.art = art


# --------------------------------------------------------------------------- parsing
ACT_RE = re.compile(r"^(องก์ที่\s*\S+)\s*[:：]\s*(.+?)\s*(\([A-Za-z].*\))?\s*$")


LIST_ITEM = re.compile(r"^\s{0,3}([-*+]|\d+\.)\s+\S")


def md_soup(text: str) -> BeautifulSoup:
    # The manuscripts often start a list on the line right after a paragraph; Markdown
    # would fold such a list into the paragraph, so give every list its blank line.
    lines, fixed = text.splitlines(), []
    for k, line in enumerate(lines):
        prev = lines[k - 1] if k else ""
        if LIST_ITEM.match(line) and prev.strip() and not LIST_ITEM.match(prev) and not prev.startswith((" ", "\t")):
            fixed.append("")
        fixed.append(line)
    h = markdown.markdown("\n".join(fixed), extensions=["tables", "sane_lists"])
    return BeautifulSoup(h, "html.parser")


def para_from(tag: Tag, typo: Typo, kind="p", **kw) -> Block:
    typo.fix_soup(tag)
    return Block(kind, inner_html(tag), split=True, **kw)


def list_blocks(lst: Tag, typo: Typo, ordered: bool) -> list[Block]:
    out = []
    n = int(lst.get("start", 1))
    for li in lst.find_all("li", recursive=False):
        marker = typo.num(n) + "." if ordered else "•"
        n += 1
        ps = li.find_all("p", recursive=False)
        parts = ps if ps else [li]
        for i, part in enumerate(parts):
            typo.fix_soup(part)
            out.append(Block("li", inner_html(part), split=True, marker=marker if i == 0 else None))
    return out


def parse_episode(path: Path, ep: int, images: Path, typo: Typo) -> dict:
    text = path.read_text(encoding="utf-8")
    title_line = next(l for l in text.splitlines() if l.startswith("## "))
    title = title_line[3:].split(":", 1)[1].strip() if ":" in title_line else title_line[3:].strip()
    body = "\n".join(l for l in text.splitlines() if not l.startswith(("# ", "## ")))
    soup = md_soup(body)
    els = [e for e in soup.children if isinstance(e, Tag)]

    def resolve(src: str) -> Path:
        p = (path.parent / src).resolve()
        return p if p.exists() else (images / Path(src).name)

    blocks: list[Block] = []
    art = None
    i = 0
    while i < len(els):
        e = els[i]
        nxt = els[i + 1] if i + 1 < len(els) else None
        img = e.find("img") if e.name == "p" else None
        if img is not None and e.get_text(strip=True) == "":
            src, alt = img["src"], img.get("alt", "")
            if art is None and not blocks:
                art = resolve(src)
            else:
                cap = alt
                if (nxt is not None and nxt.name == "p" and len(nxt.contents) == 1
                        and isinstance(nxt.contents[0], Tag) and nxt.contents[0].name == "em"):
                    cap = nxt.get_text().strip() or alt
                    i += 1
                blocks.append(Block("fig", "", src=str(resolve(src)), cap=typo.text(cap)))
            i += 1
            continue
        if e.name == "hr":
            nn = nxt
            drop = (nn is None or nn.name == "h3" or not blocks
                    or (nn.name == "p" and nn.get_text().strip().startswith("จบตอน")))
            if not drop:
                blocks.append(Block("scene", "<i></i><i></i><i></i>", space=0))
            i += 1
            continue
        if e.name == "h3":
            t = e.get_text().strip()
            m = ACT_RE.match(t)
            eb, tt = (m.group(1), m.group(2)) if m else ("", t)
            blocks.append(Block("h_act",
                                f'<div class="eb">{typo.text(eb)}</div><div class="tt">{typo.text(tt)}</div>',
                                space=1, keep=True))
            i += 1
            continue
        if e.name == "h4":
            typo.fix_soup(e)
            blocks.append(Block("h_sub", inner_html(e), space=1, keep=True))
            i += 1
            continue
        if e.name == "p" and e.get_text().strip().startswith("จบตอน"):
            strong, em = e.find("strong"), e.find("em")
            et = typo.text((strong or e).get_text().strip())
            nx = em.get_text().strip().strip("()（）") if em is not None else ""
            if not nx and nxt is not None and nxt.name == "p":
                nx = nxt.get_text().strip().strip("()（）")
                i += 1
            nx = typo.text(nx) if nx else ""
            blocks.append(Block("end",
                                f'<div class="orn"><i></i><i></i><i></i></div><div class="et">{et}</div>'
                                + (f'<div class="nx">{nx}</div>' if nx else ""), space=1))
            i += 1
            continue
        if e.name in ("ul", "ol"):
            blocks.extend(list_blocks(e, typo, e.name == "ol"))
            i += 1
            continue
        if e.name == "p":
            blocks.append(para_from(e, typo))
            i += 1
            continue
        if e.name == "blockquote":
            for p in e.find_all("p"):
                blocks.append(para_from(p, typo, cls="noindent"))
            i += 1
            continue
        print(f"  ! ep{ep}: unhandled element <{e.name}>", file=sys.stderr)
        i += 1

    main, sub = (title.split(" ", 1) + [""])[:2]
    return {"num": ep, "title": title, "main": typo.text(main), "sub": typo.text(sub),
            "art": art, "blocks": blocks}


def mark_indents(blocks: list[Block]) -> None:
    prev = None
    for b in blocks:
        if b.kind == "fig":
            continue
        if b.kind == "p" and (prev is None or prev.kind != "p"):
            b.cls = (b.cls + " noindent").strip()
        prev = b


def parse_front(path: Path, marker: str, typo: Typo) -> tuple[str, list[Block], list[dict]]:
    text = path.read_text(encoding="utf-8")
    pre = re.search(r"^## (คำนำ.*?)$(.*?)^---", text, re.S | re.M)
    head = pre.group(1)
    sub = head.split(":", 1)[1].strip() if ":" in head else ""
    soup = md_soup(pre.group(2))
    blocks: list[Block] = []
    for e in soup.children:
        if not isinstance(e, Tag):
            continue
        if e.name == "p":
            blocks.append(para_from(e, typo))
        elif e.name in ("ul", "ol"):
            blocks.extend(list_blocks(e, typo, e.name == "ol"))
    rows = []
    for line in text.splitlines():
        m = re.match(r"^\|\s*\*\*(.+?)\*\*\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$", line)
        if m:
            rows.append({"y": m.group(1), "where": m.group(2), "t": m.group(3)})
    return sub, blocks, rows


def parse_bibliography(path: Path, eps: list[int], typo: Typo) -> list[Block]:
    text = path.read_text(encoding="utf-8")
    sect = text.split("## ส่วนที่ 2")[0]
    out: list[Block] = []
    for m in re.finditer(r"^### (ตอนที่ (\d+).*?)$(.*?)(?=^### |\Z)", sect, re.S | re.M):
        if int(m.group(2)) not in eps:
            continue
        out.append(Block("h_sub", typo.text(m.group(1).strip()), space=1, keep=True))
        for line in m.group(3).splitlines():
            t = line.rstrip()
            if re.match(r"^\*\s+\*\*", t):
                topic = re.sub(r"^\*\s+\*\*(.+?)\*\*\s*$", r"\1", t).rstrip(":")
                out.append(Block("bibtopic", typo.text(topic), split=False, space=0, keep=True))
            elif re.match(r"^\s+-\s+", t):
                ent = re.sub(r"^\s+-\s+", "", t)
                h = markdown.markdown(ent)
                s = BeautifulSoup(h, "html.parser").find("p")
                typo.fix_soup(s)
                out.append(Block("bib", inner_html(s), split=True))
    return out


# --------------------------------------------------------------------------- measuring
def page_shell(css: Path, body: str, extra_css: str = "") -> str:
    return f"""<!doctype html><html lang="th"><head><meta charset="utf-8">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="{FONTS}" rel="stylesheet">
<link href="{css.as_uri()}" rel="stylesheet">
<style>{extra_css}</style></head><body>{body}</body></html>"""


MEASURE_JS = """
async (LH) => {
  await document.fonts.ready;
  const out = {};
  const rng = document.createRange();
  for (const el of document.querySelectorAll('[data-mid]')) {
    const h = el.getBoundingClientRect().height;
    const item = {h};
    if (el.dataset.split === '1') {
      const tx = el.querySelector('.tx');
      const starts = [0];
      let off = 0, cur = null;
      const w = document.createTreeWalker(tx, NodeFilter.SHOW_TEXT);
      let n;
      while ((n = w.nextNode())) {
        const s = n.data;
        for (let i = 0; i < s.length; i++) {
          rng.setStart(n, i); rng.setEnd(n, i + 1);
          const rs = rng.getClientRects();
          if (!rs.length) continue;
          const t = rs[0].top;
          if (cur === null) cur = t;
          else if (t - cur > LH / 2) { starts.push(off + i); cur = t; }
        }
        off += s.length;
      }
      item.starts = starts;
    }
    out[el.dataset.mid] = item;
  }
  return out;
}
"""

FONT_WAIT_JS = """
async () => {
  const faces = ['400','500','600','700','800'].flatMap(w => [
    document.fonts.load(`${w} 12pt "Noto Serif Thai"`, 'กขค'),
    document.fonts.load(`italic ${w} 12pt "Noto Serif Thai"`, 'กขค'),
    document.fonts.load(`${w} 12pt "Noto Sans Thai"`, 'กขค')]);
  await Promise.all(faces);
  await document.fonts.ready;
  return document.fonts.check('400 12pt "Noto Serif Thai"', 'ก') && document.fonts.check('600 12pt "Noto Sans Thai"', 'ก');
}
"""


def open_page(browser, url: str):
    pg = browser.new_page()
    pg.goto(url, wait_until="networkidle", timeout=180_000)
    if not pg.evaluate(FONT_WAIT_JS):
        raise RuntimeError("Thai web fonts did not load (network?)")
    return pg


def measure(browser, work: Path, css: Path, flows: list[Flow], extra: list[Block], align: str) -> None:
    parts = []
    for f in flows:
        items = ([f.title] if f.title else []) + f.blocks
        parts.append(f'<div class="galley">' + "".join(b.galley() for b in items if b.kind != "fig") + "</div>")
    parts.append('<div class="galley">' + "".join(b.galley() for b in extra) + "</div>")
    gal = work / "galley.html"
    gal.write_text(page_shell(css, "".join(parts), f":root{{--align:{align}}}"), encoding="utf-8")
    pg = open_page(browser, gal.as_uri())
    res = pg.evaluate(MEASURE_JS, LH_PX)
    pg.close()
    allb = [b for f in flows for b in (([f.title] if f.title else []) + f.blocks)] + extra
    bad = 0
    for b in allb:
        if b.kind == "fig":
            continue
        r = res[b.id]
        lines = r["h"] / LH_PX
        b.lines = max(1, round(lines))
        if abs(lines - b.lines) > 0.06:
            bad += 1
            print(f"  ! off-grid block {b.id} ({b.kind}): {lines:.2f} lines", file=sys.stderr)
        if b.split:
            b.starts = r["starts"]
            if len(b.starts) != b.lines:
                print(f"  ! {b.id}: {len(b.starts)} line starts vs {b.lines} lines", file=sys.stderr)
                b.split = len(b.starts) == b.lines and b.split
    if bad:
        print(f"  ! {bad} blocks are off the baseline grid", file=sys.stderr)


# --------------------------------------------------------------------------- pagination
class Frag:
    def __init__(self, block: Block, a: int, b: int, space: int):
        self.block, self.a, self.b, self.space = block, a, b, space

    def html(self) -> str:
        bl = self.block
        if bl.split:
            return bl.render(self.a, self.b if self.b < bl.lines else None,
                             first=self.a == 0, last=self.b >= bl.lines, space=self.space)
        return bl.render(0, None, True, True, space=self.space)


def paginate(flow: Flow, first_cap: int) -> list[dict]:
    pages: list[dict] = []
    cur: list[Frag] = []
    used = 0
    cap = first_cap
    pending: list[Block] = []
    blocks = flow.blocks

    def close():
        nonlocal cur, used, cap
        pages.append({"type": "text", "frags": cur, "first": not pages, "used": used, "cap": cap})
        for fb in pending:
            pages.append({"type": "fig", "block": fb})
        pending.clear()
        cur, used, cap = [], 0, LINES

    def next_text(i):
        for j in range(i + 1, len(blocks)):
            if blocks[j].kind != "fig":
                return blocks[j]
        return None

    for i, b in enumerate(blocks):
        if b.kind == "fig":
            pending.append(b)
            continue
        line = 0
        while True:
            space = b.space if cur else 0
            remaining = b.lines - line
            avail = cap - used - space
            need = remaining
            if b.keep:
                nb = next_text(i)
                if nb is not None:
                    need = remaining + nb.space + (min(ORPHANS, nb.lines) if nb.split else nb.lines)
            if need <= avail:
                cur.append(Frag(b, line, b.lines, space))
                used += space + remaining
                break
            if b.split and not b.keep:
                k = min(avail, remaining - WIDOWS)
                if k >= ORPHANS and (line > 0 or k >= ORPHANS):
                    cur.append(Frag(b, line, line + k, space))
                    used += space + k
                    line += k
                    close()
                    continue
            if b.kind == "end" and cur:
                # never strand the end-of-chapter mark: carry the last lines of text over with it
                last = cur[-1]
                if last.block.split:
                    take = WIDOWS if (last.b - last.a) >= ORPHANS + WIDOWS else (last.b - last.a)
                    if take == last.b - last.a:
                        cur.pop()
                        used -= last.space + take
                        carry = Frag(last.block, last.a, last.b, last.block.space)
                    else:
                        last.b -= take
                        used -= take
                        carry = Frag(last.block, last.b, last.b + take, 0)
                    close()
                    carry.space = 0
                    cur.append(carry)
                    used += carry.b - carry.a
                    continue
            if not cur:            # nothing fits even on an empty page: force it
                k = min(remaining, cap - used)
                cur.append(Frag(b, line, line + k, 0))
                used += k
                line += k
                if line >= b.lines:
                    break
                close()
                continue
            close()
    if cur or pending:
        close()
    return pages


# --------------------------------------------------------------------------- assembling
class Page:
    def __init__(self, kind: str, body: str = "", rh: str = "", folio: str = "",
                 cls: str = "", label: str = ""):
        self.kind, self.body, self.rh, self.folio, self.cls, self.label = kind, body, rh, folio, cls, label
        self.side = ""


def spacer(lines: int) -> str:
    return f'<div style="height:{lines * LH_PT}pt"></div>' if lines else ""


def build_book(book: dict, part: dict, flows: dict, typo: Typo, extra: dict) -> tuple[list[Page], dict]:
    body: list[Page] = []
    toc: dict = {}

    def pad_to_recto(pages: list[Page]):
        if len(pages) % 2 == 1:
            pages.append(Page("blank"))

    def emit_flow(pages: list[Page], flow: Flow, first_line: int, toc_key: str):
        title_lines = flow.title.lines if flow.title else 0
        first_cap = LINES - (first_line - 1) - title_lines - 1
        pad_to_recto(pages)
        toc[toc_key] = len(pages)
        for pg in paginate(flow, first_cap):
            if pg["type"] == "fig":
                fb = pg["block"]
                cap = fb.extra["cap"]
                pages.append(Page("fig", f'<figure><img src="{Path(fb.extra["src"]).as_uri()}" alt="">'
                                         f'<figcaption>{cap}</figcaption></figure>', cls="figpage"))
                continue
            inner = "".join(fr.html() for fr in pg["frags"])
            if pg["first"]:
                head = spacer(first_line - 1) + flow.title.render(0, None, True, True) + spacer(1)
                art = ""
                if flow.art:
                    art = f'<div class="opener-art"><img src="{Path(flow.art).as_uri()}" alt=""></div>'
                pages.append(Page("opener", head + inner, cls="opener", label=art))
            else:
                pages.append(Page("text", inner))
            pages[-1].flow = flow

    # ---- body: part opener + chapters + bibliography
    pn = typo.num(part["part"])
    name = part["name"].split(":", 1)[1].strip() if ":" in part["name"] else part["name"]
    body.append(Page("part", f'''<div class="bg"></div><div class="inner">
      <div class="eb">ภาคที่</div><div class="num">{pn}</div><div class="name">{typo.text(name)}</div>
      <div class="yrs">{typo.text(part["years"])}</div>
      <div class="rule"><i></i><i></i><i></i></div>
      <div class="desc">{typo.text(part["desc"])}</div></div>''', cls="partpage"))
    toc["part"] = 0
    items = "".join(f'<li><div class="n">{typo.num(ep["num"])}</div><div><div class="t">{ep["main"]}</div>'
                    f'<div class="s">{ep["sub"]}</div></div></li>' for ep in extra["eps"])
    body.append(Page("inpart", f'<div class="inner"><div class="eb">ตอนในภาคนี้</div><ol>{items}</ol></div>',
                     cls="inpart"))
    for ep in extra["eps"]:
        emit_flow(body, flows[f"ep{ep['num']}"], OPENER_FIRST_LINE, f"ep{ep['num']}")
    emit_flow(body, flows["bib"], SECTION_FIRST_LINE, "bib")
    if len(body) % 2:
        body.append(Page("blank"))

    for idx, p in enumerate(body):
        p.side = "recto" if idx % 2 == 0 else "verso"
        if p.kind in ("text", "fig"):
            p.folio = typo.num(idx + 1)
        if p.kind == "text":
            p.rh = p.flow.rh_recto if p.side == "recto" else p.flow.rh_verso
    return body, toc


def front_matter(book: dict, part: dict, flows: dict, typo: Typo, extra: dict, toc_body: dict) -> tuple[list[Page], dict]:
    fm: list[Page] = []
    ftoc = {}
    title = book["title"]
    t_pre, t_main = (title.split(" ", 2)[0] + " " + title.split(" ", 2)[1], title.split(" ", 2)[2]) \
        if title.count(" ") >= 2 else ("", title)
    pname = part["name"].split(":", 1)[1].strip()
    pn = typo.num(part["part"])

    fm.append(Page("fixed", f'''<div class="inner"><div class="t1">{t_pre}</div><div class="t2">{t_main}</div>
        <div class="t3">ภาคที่ {pn} · {pname}</div></div>''', cls="halftitle"))
    lis = "".join(f'''<li class="{"cur" if p["part"] == part["part"] else ""}">
        <div class="p1">ภาคที่ {typo.num(p["part"])} · {typo.text(p["years"])}</div>
        <div class="p2">{typo.text(p["name"].split(":", 1)[1].strip())}</div>
        <div class="p3">ตอนที่ {typo.num(p["episodes"][0])}–{typo.num(p["episodes"][-1])}</div></li>'''
                  for p in book["parts"])
    fm.append(Page("fixed", f'<div class="inner"><div class="eb">ชุดหนังสือ</div><div class="bt">{title}</div>'
                            f'<ol>{lis}</ol></div>', cls="series"))
    fm.append(Page("fixed", f'''<div class="inner">
        <div class="eb">{book.get("eyebrow", "")}</div>
        <div class="pre">{t_pre}</div><div class="t">{t_main}</div>
        <div class="st">{book.get("subtitle", "")}</div>
        <div class="rule"><i></i><i></i><i></i></div>
        <div class="pt">ภาคที่ {pn}</div><div class="pn">{pname}</div>
        <div class="py">{typo.text(part["years"])} · ตอนที่ {typo.num(part["episodes"][0])}–{typo.num(part["episodes"][-1])}</div>
        </div>
        <div class="foot"><div class="au ph">[ชื่อผู้เขียน]</div><div class="pub ph">[ชื่อสำนักพิมพ์]</div></div>''',
                     cls="titlepage"))
    fm.append(Page("fixed", f'''<div class="inner">
        <p class="cip">ข้อมูลทางบรรณานุกรมของหอสมุดแห่งชาติ (CIP)<br><span class="ph">[รอข้อมูลจากหอสมุดแห่งชาติ]</span></p>
        <p><b>{title}</b><br>ภาคที่ {pn} : {pname}</p>
        <p>ผู้เขียน <span class="ph">[ชื่อผู้เขียน]</span><br>
           ISBN <span class="ph">[เลข ISBN]</span><br>
           พิมพ์ครั้งที่ ๑ <span class="ph">[เดือน ปี]</span> จำนวน <span class="ph">[จำนวน]</span> เล่ม</p>
        <p>ภาพประกอบและแผนที่ <span class="ph">[ระบุผู้สร้างและที่มาของภาพ]</span><br>
           จัดรูปเล่ม ขนาด ๑๓ × ๑๘.๕ ซม. เรียงพิมพ์ด้วย Noto Serif Thai และ Noto Sans Thai</p>
        <p>เรียบเรียงจาก {typo.text(book.get("colophon_sources", ""))}</p>
        <p>สงวนลิขสิทธิ์ตามพระราชบัญญัติลิขสิทธิ์ ห้ามคัดลอก ทำซ้ำ หรือเผยแพร่ส่วนใดส่วนหนึ่งของหนังสือเล่มนี้ โดยไม่ได้รับอนุญาตเป็นลายลักษณ์อักษร</p>
        <p><span class="ph">[ชื่อและที่อยู่สำนักพิมพ์ / โรงพิมพ์]</span></p></div>''', cls="copyright"))

    def emit(flow: Flow, key: str):
        if len(fm) % 2:
            fm.append(Page("blank"))
        ftoc[key] = len(fm)
        title_lines = flow.title.lines
        first_cap = LINES - (SECTION_FIRST_LINE - 1) - title_lines - 1
        for pg in paginate(flow, first_cap):
            inner = "".join(fr.html() for fr in pg["frags"])
            if pg["first"]:
                inner = spacer(SECTION_FIRST_LINE - 1) + flow.title.render(0, None, True, True) + spacer(1) + inner
                fm.append(Page("opener", inner, cls="secopen"))
            else:
                fm.append(Page("text", inner))
            fm[-1].flow = flow

    emit(flows["preface"], "preface")
    # table of contents (single page, filled in below)
    if len(fm) % 2:
        fm.append(Page("blank"))
    ftoc["toc"] = len(fm)
    fm.append(Page("toc", "", cls="toc"))
    emit(flows["timeline"], "timeline")
    if len(fm) % 2:
        fm.append(Page("blank"))

    letter = lambda i: THAI_LETTERS[i]
    for idx, p in enumerate(fm):
        p.side = "recto" if idx % 2 == 0 else "verso"
        if p.kind in ("text", "opener", "toc"):
            p.folio = letter(idx)
        if p.kind == "text":
            p.rh = p.flow.rh_recto if p.side == "recto" else p.flow.rh_verso

    # TOC body
    def row(lead: str, page: str, cls: str) -> str:
        return f'<div class="row {cls}"><div class="lead">{lead}</div><div class="pg">{page}</div></div>'
    rows = [spacer(SECTION_FIRST_LINE - 2),
            '<div class="sec-title"><div class="eb">CONTENTS</div><div class="main">สารบัญ</div>'
            '<div class="rule"><i></i><i></i><i></i></div></div>', spacer(1)]
    rows.append(row('<span class="fm">คำนำ</span>', letter(ftoc["preface"]), "fm"))
    rows.append(row('<span class="fm">เส้นเวลา ๑๕ ปีแห่งกระดานศึก</span>', letter(ftoc["timeline"]), "fm"))
    rows.append(f'<div class="part">ภาคที่ {pn} · {pname}</div>')
    for ep in extra["eps"]:
        k = f"ep{ep['num']}"
        rows.append(f'<div class="ch"><div class="n">{typo.num(ep["num"])}</div><div>'
                    + row(f'<span class="tt">{ep["main"]}</span>', typo.num(toc_body[k] + 1), "")
                    + f'<div class="ts">{ep["sub"]}</div></div></div>')
    rows.append('<div class="back">' + row('<span class="fm">บรรณานุกรมประจำภาค</span>',
                                           typo.num(toc_body["bib"] + 1), "fm") + "</div>")
    fm[ftoc["toc"]].body = '<div class="inner">' + "".join(rows) + "</div>"
    return fm, ftoc


# --------------------------------------------------------------------------- output
def page_html(p: Page) -> str:
    rh = f'<div class="rh">{p.rh}</div>' if p.rh else ""
    fo = f'<div class="folio">{p.folio}</div>' if p.folio else ""
    if p.kind in ("text", "opener", "fig"):
        inner = f'{p.label}<div class="tb">{p.body}</div>'
    elif p.kind == "blank":
        inner = ""
    else:
        inner = p.body
    return f'<section class="page {p.side} {p.cls}">{inner}{rh}{fo}</section>'


def write_pdf(browser, work: Path, css: Path, pages: list[Page], out: Path, mode: str, align: str) -> int:
    if mode == "proof":
        sheets = "".join(page_html(p) for p in pages)
        size = f"{TRIM_W}mm {TRIM_H}mm"
        overflow_sel = ".page"
    else:
        off = BLEED + SLUG
        sw, sh = TRIM_W + 2 * off, TRIM_H + 2 * off
        size = f"{sw}mm {sh}mm"
        out_sheets = []
        for n, p in enumerate(pages, 1):
            marks = []
            L = 5.0
            for x in (off, off + TRIM_W):
                for y in (off, off + TRIM_H):
                    # horizontal mark
                    hx = x - BLEED - L if x == off else x + BLEED
                    marks.append(f'<div class="mark" style="left:{hx}mm;top:{y - 0.0353}mm;width:{L}mm;height:0.25pt"></div>')
                    vy = y - BLEED - L if y == off else y + BLEED
                    marks.append(f'<div class="mark" style="left:{x - 0.0353}mm;top:{vy}mm;width:0.25pt;height:{L}mm"></div>')
            slug = (f'<div class="slugtxt" style="left:{off}mm;top:{sh - SLUG + 3.5}mm">'
                    f'{html.escape(out.stem)} · หน้า {n}/{len(pages)} · ตัดเจียน 130 × 185 มม. · ตัดตก 3 มม.</div>')
            pg = page_html(p).replace('class="page ', 'class="page ', 1)
            out_sheets.append(
                f'<div class="sheet" style="width:{sw}mm;height:{sh}mm">'
                f'<div class="bleedbox" style="left:{SLUG}mm;top:{SLUG}mm;width:{TRIM_W + 2 * BLEED}mm;height:{TRIM_H + 2 * BLEED}mm">'
                f'<div style="position:absolute;left:{BLEED}mm;top:{BLEED}mm">{pg}</div></div>'
                + "".join(marks) + slug + "</div>")
        sheets = "".join(out_sheets)
        overflow_sel = ".page"
    extra_css = (f"@page{{size:{size};margin:0}} :root{{--align:{align}}}"
                 + (" .page{overflow:visible}" if mode == "print" else ""))
    src = work / f"{mode}.html"
    src.write_text(page_shell(css, sheets, extra_css), encoding="utf-8")
    pg = open_page(browser, src.as_uri())
    # verify: no text block may overflow its grid
    over = pg.evaluate("""() => [...document.querySelectorAll('.page')].map((p, i) => {
        const tb = p.querySelector('.tb');
        return tb && tb.scrollHeight - tb.clientHeight > 2 ? i + 1 : -1; }).filter(i => i > 0)""")
    pg.pdf(path=str(out), prefer_css_page_size=True, print_background=True)
    pg.close()
    # Chrome rounds the paper size by a fraction of a millimetre; the layout is anchored
    # top-left, so set exact boxes measured from the top-left corner.
    mm = 72 / 25.4
    doc = pymupdf.open(out)
    if mode == "proof":
        W, H = TRIM_W * mm, TRIM_H * mm
    else:
        W, H = (TRIM_W + 2 * (BLEED + SLUG)) * mm, (TRIM_H + 2 * (BLEED + SLUG)) * mm
    for page in doc:
        top = page.mediabox.y1
        page.set_mediabox(pymupdf.Rect(0, top - H, W, top))
        if mode == "print":
            b0, t0 = SLUG * mm, (SLUG + BLEED) * mm
            page.set_bleedbox(pymupdf.Rect(b0, b0, W - b0, H - b0))
            page.set_trimbox(pymupdf.Rect(t0, t0, W - t0, H - t0))
    kinds = {f[2] for i in range(doc.page_count) for f in doc.get_page_fonts(i)}
    if "Type3" in kinds:
        print("  !! Type 3 fonts in PDF (variable web fonts?) - printers may reject this file", file=sys.stderr)
    doc.save(str(out) + ".tmp", garbage=3, deflate=True)
    doc.close()
    Path(str(out) + ".tmp").replace(out)
    return len(over), over


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    ap.add_argument("--part", type=int, required=True)
    ap.add_argument("--arabic-digits", action="store_true", help="keep 0-9 instead of Thai digits")
    ap.add_argument("--align", default="justify", choices=["justify", "left"])
    ap.add_argument("--only", choices=["proof", "print", "both"], default="both")
    args = ap.parse_args()

    root = REPO / "books" / args.book
    book = json.loads((root / "book.json").read_text(encoding="utf-8"))
    part = next(p for p in book["parts"] if p["part"] == args.part)
    typo = Typo(thai_digits=not args.arabic_digits)
    images = root / "images"
    css = HERE / "pocketbook.css"
    out_dir = root / "output" / "pocketbook"
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="pocketbook_"))

    short_title = book["title"]
    flows: dict[str, Flow] = {}
    eps = []
    for n in part["episodes"]:
        path = sorted((root / "episodes").glob(f"ep{n:02d}_*.md"))[0]
        ep = parse_episode(path, n, images, typo)
        mark_indents(ep["blocks"])
        eps.append(ep)
        title = Block("title", f'<div class="ch-title"><div class="eb">ตอนที่ {typo.num(n)}</div>'
                               f'<div class="main">{ep["main"]}</div>'
                               + (f'<div class="sub">{ep["sub"]}</div>' if ep["sub"] else "")
                               + '<div class="rule"></div></div>')
        flows[f"ep{n}"] = Flow(f"ep{n}", ep["blocks"], title, "chapter",
                               rh_recto=f"ตอนที่ {typo.num(n)} · {ep['main']}", rh_verso=short_title,
                               art=ep["art"])

    sub, pblocks, rows = parse_front(root / book["front_file"], book.get("preface_marker", ""), typo)
    mark_indents(pblocks)
    flows["preface"] = Flow("preface", pblocks,
                            Block("title", f'<div class="sec-title"><div class="eb">PREFACE</div><div class="main">คำนำ</div>'
                                           f'<div class="sub">{typo.text(sub)}</div><div class="rule"><i></i><i></i><i></i></div></div>'),
                            "section", rh_recto="คำนำ", rh_verso=short_title)
    tl = []
    for k, r in enumerate(rows):
        here = f"ภาค {typo.num(args.part)} " in typo.text(r["where"]) + " "
        tag = '<span class="here">ในเล่มนี้</span>' if here else ""
        tl.append(Block("tl", f'<div class="y">พ.ศ. {typo.text(r["y"])} <span>· {typo.text(r["where"])}</span>{tag}</div>'
                              f'<div class="t">{typo.text(r["t"])}</div>', space=0 if k == 0 else 1,
                        cls="cur" if here else ""))
    flows["timeline"] = Flow("timeline", tl,
                             Block("title", '<div class="sec-title"><div class="eb">TIMELINE</div>'
                                            f'<div class="main">{typo.text(book.get("timeline_heading", "เส้นเวลา"))}</div>'
                                            '<div class="rule"><i></i><i></i><i></i></div></div>'),
                             "section", rh_recto="เส้นเวลา", rh_verso=short_title)
    flows["bib"] = Flow("bib", parse_bibliography(root / book["bibliography_file"], part["episodes"], typo),
                        Block("title", '<div class="sec-title"><div class="eb">REFERENCES</div><div class="main">บรรณานุกรม</div>'
                                       f'<div class="sub">ประจำภาคที่ {typo.num(args.part)}</div><div class="rule"><i></i><i></i><i></i></div></div>'),
                        "section", rh_recto="บรรณานุกรม", rh_verso=short_title)

    with sync_playwright() as pw:
        chrome = pw.chromium.launch(executable_path=CHROME)
        browser = chrome.new_context(user_agent=STATIC_FONT_UA)
        print("measuring galley ...")
        measure(browser, work, css, list(flows.values()), [], args.align)
        body, toc_body = build_book(book, part, flows, typo, {"eps": eps})
        fm, _ = front_matter(book, part, flows, typo, {"eps": eps}, toc_body)
        pages = fm + body
        while len(pages) % 4:          # whole sheets for the printer
            blank = Page("blank")
            blank.side = "recto" if len(pages) % 2 == 0 else "verso"
            pages.append(blank)
        stem = f"{book['slug']}_part{args.part}_pocketbook"
        modes = ["proof", "print"] if args.only == "both" else [args.only]
        for mode in modes:
            out = out_dir / f"{stem}_{mode}.pdf"
            n_over, over = write_pdf(browser, work, css, pages, out, mode, args.align)
            print(f"{mode}: {out}  ({len(pages)} pages, front matter {len(fm)}, body {len(body)})"
                  + (f"  !! overflow on pages {over}" if n_over else "  text fits every page"))
        browser.close()
        chrome.close()
    print(f"work files: {work}")


if __name__ == "__main__":
    main()
