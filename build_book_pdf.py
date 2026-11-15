"""
Build formatted book PDFs for '๑๕ ปี กระดานศึกพระเจ้าตาก' using Markdown -> HTML/CSS -> Headless Chrome (Playwright).
Follows Section 4 of แผนการรวมเล่มและออกแบบปก.md:
- ICU Thai word segmentation via Chromium
- Anti-widow/orphan CSS rules (orphans: 3; widows: 3; break-after: avoid;)
- Balanced headings (text-wrap: balance;)
- 2-pass exact Table of Contents page numbering
- Automatic pymupdf page-density audit
"""

import argparse
import base64
import glob
import os
import re
import tempfile
from pathlib import Path

import markdown
import pymupdf
from playwright.sync_api import sync_playwright

ROOT = Path(r"D:\www\Tobe-Writer")
EPISODES_DIR = ROOT / "episodes"
IMAGES_DIR = ROOT / "images"
CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

PARTS = {
    1: {
        "name": "ภาคที่ ๑ : ฝ่าไฟไปตั้งหลัก",
        "years": "พ.ศ. ๒๓๐๙ – ๒๓๑๐",
        "episodes": range(1, 6),
        "desc": "จากกองกำลังทหารม้าและทหารราบเพียง ๕๐๐ นายที่ตัดสินใจแหวกวงล้อมพม่าในคืนไฟบรรลัยกัลป์ สู่การรวบรวมหัวเมืองชายฝั่งทะเลตะวันออก ทุบหม้อข้าวตีเมืองจันทบูร และต่อเรือรบ ๑๐๐ ลำแล่นย้อนกลับมาพลิกชะตาแผ่นดินสยาม",
        "out": "กระดานศึกพระเจ้าตาก_ภาคที่1_ตอนที่1-5.pdf",
    },
    2: {
        "name": "ภาคที่ ๒ : แผ่นดินเดียวต้องมีพระเจ้าแผ่นดินองค์เดียว",
        "years": "พ.ศ. ๒๓๑๑ – ๒๓๑๓",
        "episodes": range(6, 11),
        "desc": "สงครามรวมแผ่นดิน ปราบชุมนุมทั้ง ๕ ทั่วราชอาณาจักร ตั้งแต่ศึกบางกุ้ง เกยไชย พิมาย นครศรีธรรมราช ไปจนถึงชุมนุมพระฝางแห่งสวางคบุรี",
        "out": "กระดานศึกพระเจ้าตาก_ภาคที่2_ตอนที่6-10.pdf",
    },
    3: {
        "name": "ภาคที่ ๓ : ขยายขอบขัณฑสีมาและศึกพม่า ๙ ทัพยุคธนบุรี",
        "years": "พ.ศ. ๒๓๑๔ – ๒๓๑๙",
        "episodes": range(11, 15),
        "desc": "การปะทะกับมหาอำนาจคองบอง ยุทธการกัมพูชา-ฮาเตียน วีรกรรมพระยาพิชัยดาบหัก กลศึกล้อม ๓ ชั้นที่บางแก้ว และมหาศึกพิษณุโลกกับอะแซหวุ่นกี้",
        "out": "กระดานศึกพระเจ้าตาก_ภาคที่3_ตอนที่11-14.pdf",
    },
    4: {
        "name": "ภาคที่ ๔ : เส้นทางสู่แม่น้ำโขง และฉากสุดท้ายแห่งกรุงธนบุรี",
        "years": "พ.ศ. ๒๓๑๙ – ๒๓๒๕",
        "episodes": range(15, 21),
        "desc": "การขยายอำนาจสู่ลุ่มน้ำโขง นครจำปาศักดิ์ เวียงจันทน์ อัญเชิญพระแก้วมรกต ศึกกัมพูชา กบฏพระยาสรรค์ และรุ่งอรุณแห่งกรุงรัตนโกสินทร์",
        "out": "กระดานศึกพระเจ้าตาก_ภาคที่4_ตอนที่15-20.pdf",
    },
}


def img_to_data_uri(img_path: Path) -> str:
    if not img_path.exists():
        return ""
    ext = img_path.suffix.lower().lstrip(".")
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
    data = base64.b64encode(img_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def find_episode_file(ep_num: int) -> Path:
    pattern = str(EPISODES_DIR / f"ep{ep_num:02d}_*.md")
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"Episode file not found for ep{ep_num:02d}")
    return Path(matches[0])


def parse_episode(ep_num: int) -> dict:
    path = find_episode_file(ep_num)
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

    body_md = "\n".join(body_lines).strip()
    # Replace image markdown with custom figures for clean styling
    def repl_img(match):
        alt = match.group(1).strip()
        rel = match.group(2).strip()
        img_name = Path(rel).name
        img_path = IMAGES_DIR / img_name
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
    # Convert trailing episode ending block cleanly so it renders bold/italic properly
    def repl_ending(m):
        inner_html = markdown.markdown(m.group(1), extensions=["sane_lists"])
        return f'\n\n<div class="ep-ending">{inner_html}</div>'

    body_md = re.sub(
        r"\n---\s*\n(\*\*จบตอนที่[^\n]+(?:\n\*[^\n]+)?)\s*$",
        repl_ending,
        body_md,
    )
    html = markdown.markdown(body_md, extensions=["tables", "sane_lists"])
    return {"num": ep_num, "title": title, "html": html}


def extract_preface_and_timeline() -> tuple[str, str]:
    path = ROOT / "ส่วนหน้า_คำนำและไทม์ไลน์.md"
    text = path.read_text(encoding="utf-8")
    parts = text.split("## เส้นเวลา ๑๕ ปีแห่งกระดานศึก")
    pref_md = parts[0].split("## คำนำ:")[1] if "## คำนำ:" in parts[0] else parts[0]
    pref_html = markdown.markdown("## คำนำ:" + pref_md, extensions=["tables"])
    time_html = ""
    if len(parts) > 1:
        time_html = markdown.markdown("## เส้นเวลา ๑๕ ปีแห่งกระดานศึก" + parts[1], extensions=["tables"])
    return pref_html, time_html


def extract_bibliography(ep_nums: list[int]) -> str:
    path = ROOT / "บรรณานุกรมและรายการอ้างอิง.md"
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


CSS = """
@page {
  size: 165mm 240mm;
  margin: 18mm 17mm 18mm 17mm;
  @top-center {
    content: string(book-header);
    font-family: 'Sarabun', 'Leelawadee UI', 'Tahoma', sans-serif;
    font-size: 8.5pt;
    color: #6b5e4c;
    border-bottom: 0.5pt solid #d9cfbe;
    padding-bottom: 3pt;
    margin-bottom: 6pt;
  }
  @bottom-center {
    content: "— " counter(page) " —";
    font-family: 'Sarabun', 'Leelawadee UI', 'Tahoma', sans-serif;
    font-size: 8.5pt;
    color: #6b5e4c;
  }
}

@page :first {
  @top-center { content: none; }
  @bottom-center { content: none; }
}

@page no-chrome {
  @top-center { content: none; }
  @bottom-center { content: none; }
}

* {
  box-sizing: border-box;
}

body {
  font-family: 'Noto Serif Thai', 'Sarabun', 'Leelawadee UI', 'Tahoma', sans-serif;
  font-size: 10.3pt;
  line-height: 1.63;
  color: #1f1b16;
  margin: 0;
  padding: 0;
  string-set: book-header "๑๕ ปี กระดานศึกพระเจ้าตาก";
}

.title-page {
  page: no-chrome;
  break-after: page;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  text-align: center;
  min-height: 195mm;
  border: 1.5pt solid #8c6d3f;
  padding: 20mm 14mm;
  background: #fcfaf5;
}

.eyebrow {
  font-family: 'Sarabun', 'Leelawadee UI', sans-serif;
  font-size: 10pt;
  letter-spacing: 1.5pt;
  color: #8b261d;
  margin-bottom: 8mm;
}

.book-title {
  font-family: 'Sarabun', 'Leelawadee UI', sans-serif;
  font-size: 24pt;
  font-weight: 700;
  color: #1c1813;
  line-height: 1.3;
  margin: 0 0 5mm 0;
  text-wrap: balance;
}

.book-subtitle {
  font-size: 11.5pt;
  color: #4a3f31;
  line-height: 1.55;
  max-width: 115mm;
  margin: 0 0 12mm 0;
  text-wrap: balance;
}

.part-badge {
  border-top: 1pt solid #8c6d3f;
  border-bottom: 1pt solid #8c6d3f;
  padding: 4mm 8mm;
  font-family: 'Sarabun', 'Leelawadee UI', sans-serif;
  font-size: 12pt;
  font-weight: 600;
  color: #5c4322;
  margin-bottom: 5mm;
  text-wrap: balance;
}

.part-desc {
  font-size: 9.5pt;
  color: #5e5344;
  max-width: 110mm;
  line-height: 1.6;
}

.front-section, .toc-page, .part-divider {
  break-after: page;
}

.part-divider {
  page: no-chrome;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  text-align: center;
  min-height: 190mm;
}

.part-divider h2 {
  font-family: 'Sarabun', 'Leelawadee UI', sans-serif;
  font-size: 18pt;
  color: #8b261d;
  margin: 0 0 4mm 0;
  text-wrap: balance;
}

.toc-title {
  font-family: 'Sarabun', 'Leelawadee UI', sans-serif;
  font-size: 16pt;
  font-weight: 700;
  color: #1c1813;
  border-bottom: 1.5pt solid #8c6d3f;
  padding-bottom: 3mm;
  margin-bottom: 6mm;
}

.toc-list {
  list-style: none;
  padding: 0;
  margin: 0;
}

.toc-item {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  border-bottom: 0.5pt dotted #cfc5b4;
  padding: 2.2mm 0;
  font-size: 10pt;
}

.toc-item.part-head {
  font-family: 'Sarabun', 'Leelawadee UI', sans-serif;
  font-weight: 700;
  color: #8b261d;
  border-bottom: none;
  margin-top: 4mm;
  padding-bottom: 1mm;
}

.episode {
  break-before: page;
}

.ep-header {
  border-bottom: 1.2pt solid #8c6d3f;
  padding-bottom: 3mm;
  margin-bottom: 5mm;
  break-after: avoid;
}

.ep-title {
  font-family: 'Sarabun', 'Leelawadee UI', sans-serif;
  font-size: 15.5pt;
  font-weight: 700;
  color: #1c1813;
  line-height: 1.35;
  margin: 0;
  text-wrap: balance;
}

h2, h3, h4 {
  font-family: 'Sarabun', 'Leelawadee UI', sans-serif;
  color: #7c2219;
  break-after: avoid;
  page-break-after: avoid;
  text-wrap: balance;
}

h3 {
  font-size: 12.2pt;
  margin: 6mm 0 2.5mm 0;
  border-left: 3pt solid #8c6d3f;
  padding-left: 3mm;
}

h4 {
  font-size: 10.8pt;
  color: #3d3122;
  margin: 4.5mm 0 1.8mm 0;
}

p {
  margin: 0 0 3.2mm 0;
  text-align: left;
  orphans: 3;
  widows: 3;
}

ul, ol {
  margin: 0 0 3.5mm 0;
  padding-left: 6mm;
}

li {
  margin-bottom: 1.5mm;
  orphans: 2;
  widows: 2;
}

hr {
  border: none;
  border-top: 0.6pt solid #d9cfbe;
  margin: 5mm 0;
}

figure {
  margin: 4mm 0 5mm 0;
  text-align: center;
  break-inside: avoid;
  page-break-inside: avoid;
}

figure.scene-figure img {
  width: 100%;
  max-height: 76mm;
  object-fit: cover;
  border-radius: 3pt;
  border: 0.6pt solid #c4b59b;
}

figure.map-figure img {
  width: auto;
  max-width: 100%;
  max-height: 148mm;
  border-radius: 2pt;
  border: 0.6pt solid #c4b59b;
}

figcaption {
  font-family: 'Sarabun', 'Leelawadee UI', sans-serif;
  font-size: 8.5pt;
  color: #5e5344;
  margin-top: 1.8mm;
  line-height: 1.4;
}

.missing-img {
  border: 1pt dashed #999;
  padding: 12mm 6mm;
  margin: 5mm 0;
  text-align: center;
  font-size: 9pt;
  color: #666;
  background: #f5f2eb;
  break-inside: avoid;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 8.8pt;
  margin: 4mm 0;
  break-inside: avoid;
}

th, td {
  border: 0.5pt solid #c8bca8;
  padding: 2mm 2.5mm;
  text-align: left;
  vertical-align: top;
}

th {
  background: #f0e8d8;
  font-family: 'Sarabun', 'Leelawadee UI', sans-serif;
  font-weight: 700;
}

.bib-section {
  break-before: page;
  font-size: 8.8pt;
  line-height: 1.48;
}

.ep-ending {
  margin-top: 3.5mm;
  padding-top: 2.5mm;
  border-top: 0.6pt solid #d9cfbe;
  font-size: 9.5pt;
  color: #4a3f31;
  break-inside: avoid;
}

.ep-ending p {
  margin: 0 0 1mm 0;
}

.anchor-marker {
  font-size: 0.1pt;
  color: transparent;
  user-select: none;
}
"""


def build_html(part_nums: list[int], page_map: dict[str, int] | None = None) -> str:
    is_omnibus = len(part_nums) == 4
    if is_omnibus:
        badge = "ฉบับสมบูรณ์ • ๔ ภาค ๒๐ ตอน (พ.ศ. ๒๓๐๙ – ๒๓๒๕)"
        desc = "สารคดีประวัติศาสตร์เชิงยุทธศาสตร์จากคืนตีฝ่าวงล้อมค่ายโพธิ์สามต้นจนถึงการสถาปนากรุงรัตนโกสินทร์"
    else:
        p = PARTS[part_nums[0]]
        badge = f"{p['name']}<br><span style='font-weight:400;font-size:10.5pt'>({p['years']})</span>"
        desc = p["desc"]

    pref_html, time_html = extract_preface_and_timeline()

    toc_rows = []
    if page_map is None:
        page_map = {}

    toc_rows.append(
        f'<li class="toc-item"><span>คำนำ: ห้องยุทธการและพยานในสนามรบ</span><span>{page_map.get("MARK_PREF", "—")}</span></li>'
    )
    toc_rows.append(
        f'<li class="toc-item"><span>เส้นเวลา ๑๕ ปีแห่งกระดานศึก</span><span>{page_map.get("MARK_TIME", "—")}</span></li>'
    )

    body_blocks = []
    ep_all = []
    for pnum in part_nums:
        pinfo = PARTS[pnum]
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
            ep = parse_episode(ep_num)
            ep_all.append(ep_num)
            marker = f"MARK_EP_{ep_num:02d}"
            toc_rows.append(
                f'<li class="toc-item"><span>{ep["title"]}</span><span>{page_map.get(marker, "—")}</span></li>'
            )
            body_blocks.append(
                f'<article class="episode">'
                f'<div class="anchor-marker">{marker}</div>'
                f'<header class="ep-header"><h2 class="ep-title">{ep["title"]}</h2></header>'
                f'{ep["html"]}'
                f'</article>'
            )

    toc_rows.append(
        f'<li class="toc-item part-head"><span>บรรณานุกรมและรายการอ้างอิง</span><span>{page_map.get("MARK_BIB", "—")}</span></li>'
    )
    bib_html = extract_bibliography(ep_all)

    return f"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="utf-8" />
<title>๑๕ ปี กระดานศึกพระเจ้าตาก</title>
<style>{CSS}</style>
</head>
<body>
  <section class="title-page">
    <div class="eyebrow">สารคดีประวัติศาสตร์เชิงยุทธศาสตร์</div>
    <h1 class="book-title">๑๕ ปี กระดานศึกพระเจ้าตาก</h1>
    <p class="book-subtitle">๑๕ ปีแห่งไฟสงคราม กลยุทธ์ การสร้างแผ่นดินใหม่ และชะตาแผ่นดินธนบุรี</p>
    <div class="part-badge">{badge}</div>
    <p class="part-desc">{desc}</p>
  </section>

  <section class="toc-page">
    <h2 class="toc-title">สารบัญ</h2>
    <ul class="toc-list">{''.join(toc_rows)}</ul>
  </section>

  <section class="front-section">
    <div class="anchor-marker">MARK_PREF</div>
    {pref_html}
  </section>

  <section class="front-section">
    <div class="anchor-marker">MARK_TIME</div>
    {time_html}
  </section>

  {''.join(body_blocks)}

  <section class="bib-section">
    <div class="anchor-marker">MARK_BIB</div>
    <h2 class="toc-title">บรรณานุกรมและรายการอ้างอิง</h2>
    {bib_html}
  </section>
</body>
</html>"""


def render_pdf(html: str, out_path: Path):
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


def find_markers(pdf_path: Path, part_nums: list[int]) -> dict[str, int]:
    doc = pymupdf.open(str(pdf_path))
    targets = {
        "MARK_PREF": "ห้องยุทธการและพยานในสนามรบ",
        "MARK_TIME": "เส้นเวลา ๑๕ ปีแห่งกระดานศึก",
        "MARK_BIB": "บรรณานุกรมและรายการอ้างอิง",
    }
    for pnum in part_nums:
        for ep_num in PARTS[pnum]["episodes"]:
            targets[f"MARK_EP_{ep_num:02d}"] = f"ตอนที่ {ep_num}:"

    markers = {}
    # Skip page 0 (title) and page 1 (TOC)
    for i in range(2, len(doc)):
        txt = doc[i].get_text("text")
        for key, needle in targets.items():
            if key not in markers and needle in txt:
                # For MARK_BIB, require it to be a standalone heading (not inside preface/TOC)
                if key == "MARK_BIB" and i < 5:
                    continue
                markers[key] = i + 1
    doc.close()
    return markers


def audit_pdf(pdf_path: Path):
    doc = pymupdf.open(str(pdf_path))
    sparse = []
    for i, page in enumerate(doc):
        if i <= 2:
            continue
        txt = page.get_text("text").strip()
        lines = [l.strip() for l in txt.splitlines() if l.strip() and not l.strip().startswith("— ") and "กระดานศึกพระเจ้าตาก" not in l]
        imgs = page.get_images()
        if len(lines) <= 4 and len(imgs) == 0:
            sparse.append((i + 1, len(lines), lines))
    print(f"Built {pdf_path.name}: {len(doc)} pages | sparse non-image pages (<=4 lines): {len(sparse)}")
    for pno, nlines, sample in sparse:
        print(f"  [WARN] Page {pno}: {nlines} lines -> {sample[:2]}")
    doc.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--part", type=int, choices=[1, 2, 3, 4], help="Build specific part 1-4")
    parser.add_argument("--all", action="store_true", help="Build 20-episode omnibus edition")
    args = parser.parse_args()

    if args.all:
        part_nums = [1, 2, 3, 4]
        out_path = ROOT / "๑๕_ปี_กระดานศึกพระเจ้าตาก_ฉบับสมบูรณ์_20ตอน.pdf"
    elif args.part:
        part_nums = [args.part]
        out_path = ROOT / PARTS[args.part]["out"]
    else:
        part_nums = [1]
        out_path = ROOT / PARTS[1]["out"]

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tmp_pdf = Path(tf.name)
    try:
        html1 = build_html(part_nums, page_map=None)
        render_pdf(html1, tmp_pdf)
        markers = find_markers(tmp_pdf, part_nums)
        html2 = build_html(part_nums, page_map=markers)
        try:
            render_pdf(html2, out_path)
        except PermissionError:
            out_path = out_path.with_name(out_path.stem + "_v2" + out_path.suffix)
            print(f"[INFO] Original file is locked/open; writing to {out_path.name} instead.")
            render_pdf(html2, out_path)
    finally:
        tmp_pdf.unlink(missing_ok=True)

    audit_pdf(out_path)


if __name__ == "__main__":
    main()
