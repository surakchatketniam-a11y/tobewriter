"""
Multi-book PDF builder for Tobe-Writer historical series.
Reads metadata from books/<book_id>/book.json and outputs print-grade PDFs to books/<book_id>/output/.
"""

import argparse
import base64
import glob
import json
import re
import tempfile
from pathlib import Path

import markdown
import pymupdf
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOKS_DIR = REPO_ROOT / "books"
CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def img_to_data_uri(img_path: Path) -> str:
    if not img_path.exists():
        return ""
    ext = img_path.suffix.lower().lstrip(".")
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
    data = base64.b64encode(img_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def find_episode_file(episodes_dir: Path, ep_num: int) -> Path:
    pattern = str(episodes_dir / f"ep{ep_num:02d}_*.md")
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"Episode file not found for ep{ep_num:02d} in {episodes_dir}")
    return Path(matches[0])


def parse_episode(episodes_dir: Path, images_dir: Path, ep_num: int) -> dict:
    path = find_episode_file(episodes_dir, ep_num)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    title = f"ตอนที่ {ep_num}"
    body_lines = []
    for line in lines:
        if line.startswith("# ภาคที่ "):
            continue
        if line.startswith("## ตอนที่ "):
            title = line.lstrip("# ").strip()
            continue
        body_lines.append(line)

    ep_eyebrow = f"ตอนที่ {ep_num}"
    ep_maintitle = title
    if ":" in title:
        left, right = title.split(":", 1)
        ep_eyebrow = left.strip()
        ep_maintitle = right.strip()

    body_md = "\n".join(body_lines).strip()

    def repl_img(match):
        alt = match.group(1).strip()
        rel = match.group(2).strip()
        img_name = Path(rel).name
        img_path = images_dir / img_name
        if not img_path.exists():
            return f'<div class="missing-img">[รอภาพประกอบ: {img_name} — {alt}]</div>'
        uri = img_to_data_uri(img_path)
        cls = "map-figure" if "_map" in img_name else "scene-figure"
        return (
            f'<figure class="{cls}">'
            f'<img src="{uri}" alt="{alt}" />'
            f'<figcaption>{alt}</figcaption>'
            f"</figure>"
        )

    body_md = re.sub(r"!\[(.*?)\]\((.*?)\)", repl_img, body_md)
    body_md = re.sub(
        r"\n---\s*\n\*\*จบตอนที่[^\n]+(?:\n\*[^\n]+)?\s*$",
        '\n\n<div class="ep-ornament">❖ &nbsp; ❖ &nbsp; ❖</div>',
        body_md,
    )
    body_md = re.sub(
        r"^(### [^\n(]+)(\([^\n)]+\))\s*$",
        r'\1<span class="act-en">\2</span>',
        body_md,
        flags=re.MULTILINE,
    )
    html = markdown.markdown(body_md, extensions=["tables", "sane_lists"])
    return {
        "num": ep_num,
        "title": title,
        "eyebrow": ep_eyebrow,
        "maintitle": ep_maintitle,
        "html": html,
    }


def extract_preface_and_timeline(book_dir: Path, cfg: dict) -> tuple[str, str]:
    path = book_dir / cfg.get("front_file", "ส่วนหน้า_คำนำและไทม์ไลน์.md")
    if not path.exists():
        return "", ""
    text = path.read_text(encoding="utf-8")
    tl_heading = cfg.get("timeline_heading", "เส้นเวลา")
    marker = f"## {tl_heading}"
    parts = text.split(marker)
    pref_md = parts[0].split("## คำนำ:")[1] if "## คำนำ:" in parts[0] else parts[0]
    pref_html = markdown.markdown("## คำนำ:" + pref_md, extensions=["tables"])
    time_html = ""
    if len(parts) > 1:
        time_html = markdown.markdown(marker + parts[1], extensions=["tables"])
    return pref_html, time_html


def extract_bibliography(book_dir: Path, cfg: dict, ep_nums: list[int]) -> str:
    path = book_dir / cfg.get("bibliography_file", "บรรณานุกรมและรายการอ้างอิง.md")
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    sections = re.split(r"(?=^### ตอนที่ )", text, flags=re.MULTILINE)
    selected = []
    for sec in sections:
        m = re.match(r"^### ตอนที่ (\d+)", sec)
        if m and int(m.group(1)) in ep_nums:
            selected.append(sec.strip())
    master_split = text.split("## ส่วนที่ 2: บรรณานุกรมรวม")
    master_md = "## ส่วนที่ 2: บรรณานุกรมรวม" + master_split[1] if len(master_split) > 1 else ""
    combined_md = "\n\n".join(selected) + "\n\n---\n\n" + master_md
    return markdown.markdown(combined_md, extensions=["tables", "sane_lists"])


def build_css(book_title: str, right_header: str) -> str:
    return f"""
@page {{
  size: 165mm 240mm;
  margin: 20mm 16mm 20mm 22mm;
  @top-left {{ content: none; }}
  @top-right {{ content: none; }}
  @bottom-center {{
    content: counter(page);
    font-family: 'Sarabun', 'Leelawadee UI', 'Tahoma', sans-serif;
    font-size: 8.5pt;
    color: #5e5344;
  }}
}}

@page :left {{
  margin: 20mm 22mm 20mm 16mm;
  @top-left {{
    content: "{book_title}";
    font-family: 'Sarabun', 'Leelawadee UI', 'Tahoma', sans-serif;
    font-size: 8pt;
    color: #7a6d5b;
    border-bottom: 0.4pt solid #d8cebe;
    padding-bottom: 2.5pt;
    margin-bottom: 5pt;
  }}
  @top-right {{
    content: "";
    border-bottom: 0.4pt solid #d8cebe;
    padding-bottom: 2.5pt;
    margin-bottom: 5pt;
  }}
}}

@page :right {{
  margin: 20mm 16mm 20mm 22mm;
  @top-left {{
    content: "";
    border-bottom: 0.4pt solid #d8cebe;
    padding-bottom: 2.5pt;
    margin-bottom: 5pt;
  }}
  @top-right {{
    content: "{right_header}";
    font-family: 'Sarabun', 'Leelawadee UI', 'Tahoma', sans-serif;
    font-size: 8pt;
    color: #7a6d5b;
    border-bottom: 0.4pt solid #d8cebe;
    padding-bottom: 2.5pt;
    margin-bottom: 5pt;
  }}
}}

@page :first {{
  @top-left {{ content: none; }}
  @top-right {{ content: none; }}
  @bottom-center {{ content: none; }}
}}

@page no-chrome {{
  @top-left {{ content: none; }}
  @top-right {{ content: none; }}
  @bottom-center {{ content: none; }}
}}

@page chapter-start {{
  @top-left {{ content: none; }}
  @top-right {{ content: none; }}
}}

* {{ box-sizing: border-box; }}

body {{
  font-family: 'Noto Serif Thai', 'Leelawadee UI', 'Tahoma', serif;
  font-size: 10.2pt;
  line-height: 1.72;
  color: #1c1915;
  margin: 0;
  padding: 0;
}}

.title-page {{
  page: no-chrome;
  break-after: page;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  text-align: center;
  min-height: 196mm;
  border: 1.5pt solid #8c6d3f;
  outline: 0.5pt solid #8c6d3f;
  outline-offset: -3.5pt;
  padding: 20mm 14mm;
  background: #fcfaf5;
}}

.title-page p {{
  text-indent: 0;
  text-align: center;
}}

.colophon-page {{
  page: no-chrome;
  break-after: page;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  min-height: 196mm;
  font-family: 'Sarabun', 'Leelawadee UI', sans-serif;
  font-size: 8.8pt;
  line-height: 1.65;
  color: #4a3f31;
  padding-bottom: 8mm;
}}

.colophon-box {{
  border-top: 1pt solid #8c6d3f;
  padding-top: 5mm;
}}

.eyebrow {{
  font-family: 'Sarabun', 'Leelawadee UI', sans-serif;
  font-size: 9.5pt;
  letter-spacing: 1.5pt;
  color: #8b261d;
  margin-bottom: 7mm;
}}

.book-title {{
  font-family: 'Noto Serif Thai', 'Sarabun', 'Leelawadee UI', serif;
  font-size: 25pt;
  font-weight: 700;
  color: #1c1813;
  line-height: 1.3;
  margin: 0 0 4.5mm 0;
  text-wrap: balance;
}}

.book-subtitle {{
  font-family: 'Sarabun', 'Leelawadee UI', sans-serif;
  font-size: 11pt;
  color: #4a3f31;
  line-height: 1.55;
  max-width: 115mm;
  margin: 0 0 11mm 0;
  text-wrap: balance;
}}

.part-badge {{
  border-top: 1pt solid #8c6d3f;
  border-bottom: 1pt solid #8c6d3f;
  padding: 4mm 8mm;
  font-family: 'Sarabun', 'Leelawadee UI', sans-serif;
  font-size: 12pt;
  font-weight: 600;
  color: #5c4322;
  margin-bottom: 5mm;
  text-wrap: balance;
}}

.part-desc {{
  font-size: 9.4pt;
  color: #5e5344;
  max-width: 110mm;
  line-height: 1.65;
}}

.front-section, .toc-page, .part-divider {{
  break-after: page;
}}

.front-section p, .bib-section p {{
  text-indent: 0;
  margin-bottom: 2.5mm;
}}

.part-divider {{
  page: no-chrome;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  text-align: center;
  min-height: 190mm;
}}

.part-divider h2 {{
  font-family: 'Noto Serif Thai', 'Sarabun', sans-serif;
  font-size: 18pt;
  color: #8b261d;
  margin: 0 0 4mm 0;
  text-wrap: balance;
}}

.toc-title {{
  font-family: 'Sarabun', 'Leelawadee UI', sans-serif;
  font-size: 15.5pt;
  font-weight: 700;
  color: #1c1813;
  border-bottom: 1.5pt solid #8c6d3f;
  padding-bottom: 2.5mm;
  margin-bottom: 6mm;
}}

.toc-list {{
  list-style: none;
  padding: 0;
  margin: 0;
}}

.toc-item {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  border-bottom: 0.5pt dotted #cfc5b4;
  padding: 2mm 0;
  font-family: 'Sarabun', 'Leelawadee UI', sans-serif;
  font-size: 9.8pt;
}}

.toc-item.part-head {{
  font-weight: 700;
  color: #8b261d;
  border-bottom: none;
  margin-top: 4mm;
  padding-bottom: 1mm;
}}

.episode {{
  break-before: page;
  page: chapter-start;
}}

.ep-header {{
  text-align: center;
  border-bottom: 1pt solid #8c6d3f;
  padding-bottom: 4mm;
  margin-bottom: 5mm;
  break-after: avoid;
}}

.ep-eyebrow {{
  font-family: 'Sarabun', 'Leelawadee UI', sans-serif;
  font-size: 9.5pt;
  font-weight: 700;
  letter-spacing: 1pt;
  color: #8b261d;
  margin-bottom: 1.5mm;
}}

.ep-title {{
  font-family: 'Noto Serif Thai', 'Sarabun', sans-serif;
  font-size: 16pt;
  font-weight: 700;
  color: #1c1813;
  line-height: 1.35;
  margin: 0;
  text-wrap: balance;
}}

h2, h3, h4 {{
  font-family: 'Sarabun', 'Leelawadee UI', sans-serif;
  color: #7c2219;
  break-after: avoid;
  page-break-after: avoid;
}}

h3 {{
  font-size: 11pt;
  font-weight: 700;
  line-height: 1.4;
  margin: 5.5mm 0 2.5mm 0;
  border-left: 3pt solid #8c6d3f;
  padding-left: 3mm;
}}

.act-en {{
  font-weight: 600;
  font-size: 9.8pt;
  color: #6b5e4c;
  white-space: nowrap;
}}

h4 {{
  font-size: 10.5pt;
  font-weight: 700;
  color: #3d3122;
  margin: 4mm 0 1.5mm 0;
}}

p {{
  margin: 0 0 1.2mm 0;
  text-indent: 7.5mm;
  text-align: left;
  orphans: 3;
  widows: 3;
}}

h2 + p, h3 + p, h4 + p, figure + p, hr + p, blockquote + p, ul + p, ol + p {{
  text-indent: 0;
}}

blockquote {{
  margin: 3mm 6mm;
  padding: 2mm 4mm;
  border-left: 2pt solid #8c6d3f;
  background: #faf7f0;
  font-style: italic;
  break-inside: avoid;
}}

blockquote p {{
  text-indent: 0;
  margin: 0;
}}

ul, ol {{
  margin: 1.5mm 0 2.5mm 0;
  padding-left: 7mm;
}}

li {{
  margin-bottom: 1.2mm;
  orphans: 2;
  widows: 2;
}}

hr {{
  border: none;
  border-top: 0.5pt solid #d9cfbe;
  width: 40%;
  margin: 4.5mm auto;
}}

figure {{
  margin: 3.5mm 0 4.5mm 0;
  text-align: center;
  break-inside: avoid;
  page-break-inside: avoid;
}}

figure.scene-figure img {{
  width: 100%;
  max-height: 72mm;
  object-fit: cover;
  border-radius: 2pt;
  border: 0.6pt solid #c4b59b;
}}

figure.map-figure img {{
  width: auto;
  max-width: 100%;
  max-height: 136mm;
  border-radius: 2pt;
  border: 0.6pt solid #c4b59b;
}}

figcaption {{
  font-family: 'Sarabun', 'Leelawadee UI', sans-serif;
  font-size: 8.3pt;
  color: #5e5344;
  margin-top: 1.5mm;
  line-height: 1.4;
  text-align: center;
}}

.missing-img {{
  border: 1pt dashed #999;
  padding: 12mm 6mm;
  margin: 5mm 0;
  text-align: center;
  font-size: 9pt;
  color: #666;
  background: #f5f2eb;
  break-inside: avoid;
}}

table {{
  width: 100%;
  border-collapse: collapse;
  font-family: 'Sarabun', 'Leelawadee UI', sans-serif;
  font-size: 8.5pt;
  margin: 3.5mm 0;
  break-inside: avoid;
}}

th, td {{
  border: 0.5pt solid #c8bca8;
  padding: 1.8mm 2.2mm;
  text-align: left;
  vertical-align: top;
}}

th {{
  background: #f0e8d8;
  font-weight: 700;
}}

.bib-section {{
  break-before: page;
  font-family: 'Sarabun', 'Leelawadee UI', sans-serif;
  font-size: 8.8pt;
  line-height: 1.5;
}}

.ep-ornament {{
  margin-top: 5mm;
  text-align: center;
  font-size: 9pt;
  color: #8c6d3f;
  letter-spacing: 2pt;
}}
"""


def build_html(book_dir: Path, cfg: dict, selected_parts: list[dict], page_map: dict[str, int] | None = None) -> str:
    episodes_dir = book_dir / "episodes"
    images_dir = book_dir / "images"
    is_omnibus = len(selected_parts) > 1
    if is_omnibus:
        badge = cfg["omnibus_badge"]
        desc = cfg["omnibus_desc"]
        right_header = cfg["eyebrow"]
    else:
        p = selected_parts[0]
        badge = f"{p['name']}<br><span style='font-weight:400;font-size:10.5pt'>({p['years']})</span>"
        desc = p["desc"]
        right_header = p["name"]

    pref_html, time_html = extract_preface_and_timeline(book_dir, cfg)
    if page_map is None:
        page_map = {}

    toc_rows = []
    if pref_html:
        toc_rows.append(
            f'<li class="toc-item"><span>คำนำ: {cfg.get("preface_marker", "คำนำ")}</span><span>{page_map.get("MARK_PREF", "—")}</span></li>'
        )
    if time_html:
        toc_rows.append(
            f'<li class="toc-item"><span>{cfg.get("timeline_heading", "เส้นเวลา")}</span><span>{page_map.get("MARK_TIME", "—")}</span></li>'
        )

    body_blocks = []
    ep_all = []
    for pinfo in selected_parts:
        toc_rows.append(f'<li class="toc-item part-head"><span>{pinfo["name"]} ({pinfo["years"]})</span><span></span></li>')
        if is_omnibus:
            body_blocks.append(
                f'<section class="part-divider">'
                f'<div class="eyebrow">{pinfo["years"]}</div>'
                f'<h2>{pinfo["name"]}</h2>'
                f'<p class="part-desc">{pinfo["desc"]}</p>'
                f'</section>'
            )
        for ep_num in pinfo["episodes"]:
            ep = parse_episode(episodes_dir, images_dir, ep_num)
            ep_all.append(ep_num)
            marker = f"MARK_EP_{ep_num:02d}"
            toc_rows.append(
                f'<li class="toc-item"><span>{ep["title"]}</span><span>{page_map.get(marker, "—")}</span></li>'
            )
            body_blocks.append(
                f'<article class="episode">'
                f'<header class="ep-header">'
                f'<div class="ep-eyebrow">{ep["eyebrow"]}</div>'
                f'<h2 class="ep-title">{ep["maintitle"]}</h2>'
                f'</header>'
                f'{ep["html"]}'
                f'</article>'
            )

    bib_html = extract_bibliography(book_dir, cfg, ep_all)
    if bib_html:
        toc_rows.append(
            f'<li class="toc-item part-head"><span>บรรณานุกรมและรายการอ้างอิง</span><span>{page_map.get("MARK_BIB", "—")}</span></li>'
        )

    css = build_css(cfg["title"], right_header)
    front_blocks = ""
    if pref_html:
        front_blocks += f'<section class="front-section">{pref_html}</section>'
    if time_html:
        front_blocks += f'<section class="front-section">{time_html}</section>'
    bib_block = f'<section class="bib-section"><h2 class="toc-title">บรรณานุกรมและรายการอ้างอิง</h2>{bib_html}</section>' if bib_html else ""

    return f"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="utf-8" />
<title>{cfg["title"]}</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+Thai:wght@400;600;700&family=Sarabun:ital,wght@0,400;0,600;0,700;1,400&display=swap" rel="stylesheet" />
<style>{css}</style>
</head>
<body>
  <section class="title-page">
    <div class="eyebrow">{cfg["eyebrow"]}</div>
    <h1 class="book-title">{cfg["title"]}</h1>
    <p class="book-subtitle">{cfg["subtitle"]}</p>
    <div class="part-badge">{badge}</div>
    <p class="part-desc">{desc}</p>
  </section>

  <section class="colophon-page">
    <div class="colophon-box">
      <strong>{cfg["title"]}</strong><br />
      <em>{cfg["subtitle"]}</em><br /><br />
      {badge}<br />
      แนวคิดการเล่าเรื่อง: ห้องยุทธการและพยานในสนามรบ (Map-based Storytelling)<br />
      อ้างอิงหลักฐานชั้นต้น: {cfg.get("colophon_sources", "")}<br />
      ขนาดรูปเล่มมาตรฐาน: ๑๖๕ × ๒๔๐ มม. (ขอบสันเย็บเล่ม ๒๒ มม. / ขอบนอก ๑๖ มม.)<br />
      แบบอักษร: Noto Serif Thai (เนื้อหา) และ Sarabun (หัวข้อและคำบรรยาย)
    </div>
  </section>

  <section class="toc-page">
    <h2 class="toc-title">สารบัญ</h2>
    <ul class="toc-list">{''.join(toc_rows)}</ul>
  </section>

  {front_blocks}
  {''.join(body_blocks)}
  {bib_block}
</body>
</html>"""


def render_pdf(html: str, out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as tf:
        tf.write(html)
        html_path = Path(tf.name)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROME_EXE, headless=True)
            page = browser.new_page()
            page.goto(html_path.as_uri(), wait_until="networkidle")
            page.pdf(path=str(out_path), prefer_css_page_size=True, print_background=True)
            browser.close()
    finally:
        html_path.unlink(missing_ok=True)


def find_markers(pdf_path: Path, book_dir: Path, cfg: dict, selected_parts: list[dict]) -> dict[str, int]:
    doc = pymupdf.open(str(pdf_path))
    episodes_dir = book_dir / "episodes"
    images_dir = book_dir / "images"
    targets = {
        "MARK_PREF": cfg.get("preface_marker", "คำนำ"),
        "MARK_TIME": cfg.get("timeline_heading", "เส้นเวลา"),
        "MARK_BIB": "บรรณานุกรมและรายการอ้างอิง",
    }
    for pinfo in selected_parts:
        for ep_num in pinfo["episodes"]:
            ep = parse_episode(episodes_dir, images_dir, ep_num)
            targets[f"MARK_EP_{ep_num:02d}"] = ep["maintitle"][:24]

    markers = {}
    for i in range(3, len(doc)):
        txt = " ".join(doc[i].get_text("text").split())
        for key, needle in targets.items():
            if key not in markers and needle in txt:
                if key == "MARK_BIB" and i < 6:
                    continue
                markers[key] = i + 1
    doc.close()
    return markers


def audit_pdf(pdf_path: Path, book_title: str):
    doc = pymupdf.open(str(pdf_path))
    sparse = []
    for i, page in enumerate(doc):
        if i <= 2:
            continue
        txt = page.get_text("text").strip()
        lines = [l.strip() for l in txt.splitlines() if l.strip() and not l.strip().isdigit() and book_title not in l]
        imgs = page.get_images()
        if len(lines) <= 4 and len(imgs) == 0:
            sparse.append((i + 1, len(lines), lines))
    print(f"Built {pdf_path.name}: {len(doc)} pages | sparse non-image pages (<=4 lines): {len(sparse)}")
    for pno, nlines, sample in sparse:
        print(f"  [WARN] Page {pno}: {nlines} lines -> {sample[:2]}")
    doc.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--book", required=True, help="Book folder under books/ (e.g. 01_กระดานศึกพระเจ้าตาก)")
    parser.add_argument("--part", type=int, help="Part number to build (as defined in book.json)")
    parser.add_argument("--all", action="store_true", help="Build full omnibus edition")
    args = parser.parse_args()

    book_dir = BOOKS_DIR / args.book
    cfg = json.loads((book_dir / "book.json").read_text(encoding="utf-8"))
    parts = cfg.get("parts", [])
    if not parts:
        raise ValueError(f"No parts configured in {book_dir / 'book.json'}")

    if args.all:
        selected = parts
        out_path = book_dir / "output" / cfg["omnibus_out"]
    else:
        pnum = args.part or 1
        matches = [p for p in parts if p["part"] == pnum]
        if not matches:
            raise ValueError(f"Part {pnum} not found in {book_dir / 'book.json'}")
        selected = matches
        out_path = book_dir / "output" / matches[0]["out"]

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tmp_pdf = Path(tf.name)
    try:
        html1 = build_html(book_dir, cfg, selected, page_map=None)
        render_pdf(html1, tmp_pdf)
        markers = find_markers(tmp_pdf, book_dir, cfg, selected)
        html2 = build_html(book_dir, cfg, selected, page_map=markers)
        try:
            render_pdf(html2, out_path)
        except PermissionError:
            out_path = out_path.with_name(out_path.stem + "_v2" + out_path.suffix)
            print(f"[INFO] Original file is locked/open; writing to {out_path.name} instead.")
            render_pdf(html2, out_path)
    finally:
        tmp_pdf.unlink(missing_ok=True)

    audit_pdf(out_path, cfg["title"])


if __name__ == "__main__":
    main()
