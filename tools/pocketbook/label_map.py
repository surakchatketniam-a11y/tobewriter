"""Put Thai labels on a text-free base map.

AI image models garble Thai lettering, so maps are generated with no text at all and the
labels are typeset here with the book's own fonts (Noto Serif/Sans Thai) in headless Chrome.

    python tools/pocketbook/label_map.py books/02_พระนเรศวร/images/src/ep04_map.labels.json

The spec (JSON) gives coordinates in pixels of the *base* image; the output is resampled to
`out_width` (default 1536) so the labels stay sharp whatever size the base came in at.

    {
      "base": "ep04_map_base.png",            # relative to the spec file
      "out": "../ep04_map.png",               # relative to the spec file
      "banner": {"x": 543, "y": 72, "w": 560, "kicker": "...", "title": "..."},
      "markers": [{"n": 1, "x": 811, "y": 563, "r": 26, "label": "กรุงละแวก",
                   "sub": "(กัมพูชา)", "pos": "below|above|left|right", "gap": 10}],
      "texts":   [{"x": 300, "y": 1190, "text": "อ่าวไทย", "size": 30, "style": "water|region|note",
                   "rotate": 0}],
      "legend":  {"x": 445, "y": 1345, "cols": 2, "title": "...", "items": ["...", "..."],
                  "keys": [{"line": "solid|dashed", "color": "#a3221b", "text": "..."}]}
    }
"""
import argparse
import html
import json
from pathlib import Path

from PIL import Image, ImageFilter
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
FONTS = ("https://fonts.googleapis.com/css2?"
         "family=Noto+Serif+Thai:wght@500;600;700;800&family=Noto+Sans+Thai:wght@500;600;700&display=block")
THAI_DIGITS = str.maketrans("0123456789", "๐๑๒๓๔๕๖๗๘๙")

CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body { width: %(w)spx; height: %(h)spx; position: relative; overflow: hidden; background: #e9dcc0; }
img.base { position: absolute; left: 0; top: 0; width: %(w)spx; height: %(h)spx; }
.abs { position: absolute; white-space: nowrap; }
.halo { text-shadow: 0 0 3px #f1e4c6, 0 0 3px #f1e4c6, 0 0 6px #f1e4c6, 0 0 10px rgba(241,228,198,.9); }
.num { position: absolute; transform: translate(-50%%, -52%%); font: 700 %(numsize)spx/1 "Noto Sans Thai";
       color: #f3dfa6; }
.lab { font: 700 %(labsize)spx/1.15 "Noto Serif Thai"; color: #2a1c12; text-align: center; }
.lab .sub { display: block; font-weight: 600; font-size: .72em; color: #4a3424; }
.water { font: italic 600 %(watersize)spx/1 "Noto Serif Thai"; color: #24505a; letter-spacing: .02em; }
.region { font: 800 %(regionsize)spx/1 "Noto Serif Thai"; color: rgba(74,47,28,.78); letter-spacing: .12em; }
.note { font: 600 %(notesize)spx/1.15 "Noto Serif Thai"; color: #3a2718; text-align: center; }
.banner { transform: translate(-50%%, -50%%); text-align: center; color: #2a1c12; }
.banner .k { font: 600 %(kickersize)spx/1.2 "Noto Sans Thai"; color: #7a2a1d; }
.banner .t { font: 800 %(titlesize)spx/1.2 "Noto Serif Thai"; }
.legend { transform: translate(-50%%, -50%%); display: grid; column-gap: 26px; row-gap: 4px;
          font: 600 %(legendsize)spx/1.3 "Noto Serif Thai"; color: #2a1c12; }
.legend .h { grid-column: 1 / -1; text-align: center; font-weight: 800; color: #7a2a1d; margin-bottom: 4px; }
.legend b { color: #7a2a1d; margin-right: .35em; }
/* atlas-plate style (Book 1): navy badges, typeset header and summary box */
.badge { position: absolute; transform: translate(-50%%, -50%%); width: %(badge)spx; height: %(badge)spx;
         border-radius: 50%%; background: #1F2A44; border: 2px solid #fff; box-shadow: 0 0 0 1px #1F2A44;
         color: #fff; font: 700 %(badgefont)spx/%(badge)spx "Noto Sans Thai"; text-align: center; }
.badge span { position: relative; top: -1px; }
.legend.badges { row-gap: 9px; column-gap: 46px; }
.legend.badges b { display: inline-block; width: 1.45em; height: 1.45em; line-height: 1.45em; border-radius: 50%%;
                   background: #1F2A44; color: #fff; text-align: center; font: 700 .82em/1.75em "Noto Sans Thai";
                   margin-right: .55em; vertical-align: .05em; }
.legend.badges .h { color: #2a1c12; font-size: 1.12em; margin-bottom: 8px; }
.hdr { transform: translate(-50%%, -50%%); text-align: center; color: #1f1b17; white-space: normal; }
.hdr .k { font: 600 %(kickersize)spx/1.3 "Noto Sans Thai"; color: #9E2B25; }
.hdr .t { font: 800 %(titlesize)spx/1.25 "Noto Serif Thai"; margin-top: 6px; }
.hdr .s { font: 600 %(subsize)spx/1.35 "Noto Sans Thai"; color: #3a332c; margin-top: 8px; }
.hdr .s i { color: #9E2B25; font-style: normal; }
.hdr hr { border: 0; border-top: 1.5px solid #9E2B25; width: 92%%; margin: 12px auto 0; }
.summary { transform: translate(-50%%, -50%%); text-align: center; white-space: normal; color: #2a1c12; }
.summary .h { font: 800 %(sumhead)spx/1.3 "Noto Serif Thai"; }
.summary .b { font: 500 %(sumsize)spx/1.45 "Noto Sans Thai"; margin-top: 4px; }
"""


def esc(s):
    return html.escape(str(s).translate(THAI_DIGITS))


def marker_html(m, sizes, badges=False):
    x, y, r = m["x"], m["y"], m.get("r", 26)
    if badges:   # draw our own numbered badge over (or instead of) the base map's circle
        out = [f'<div class="badge" style="left:{x}px;top:{y}px"><span>{esc(m["n"])}</span></div>']
    else:
        out = [f'<div class="num" style="left:{x}px;top:{y}px;font-size:{m.get("numsize", sizes["numsize"])}px">'
               f'{esc(m["n"])}</div>']
    if m.get("label"):
        gap = r + m.get("gap", 8)
        pos = m.get("pos", "below")
        tf = {"below": (x, y + gap, "-50%", "0"), "above": (x, y - gap, "-50%", "-100%"),
              "left": (x - gap, y, "-100%", "-50%"), "right": (x + gap, y, "0", "-50%")}[pos]
        lx, ly, tx, ty = tf
        lx += m.get("dx", 0); ly += m.get("dy", 0)
        align = {"left": "right", "right": "left"}.get(pos, "center")
        sub = f'<span class="sub">{esc(m["sub"])}</span>' if m.get("sub") else ""
        out.append(f'<div class="abs lab halo" style="left:{lx}px;top:{ly}px;transform:translate({tx},{ty});'
                   f'text-align:{align}">{esc(m["label"])}{sub}</div>')
    return "".join(out)


def text_html(t):
    style = t.get("style", "note")
    size = f'font-size:{t["size"]}px;' if t.get("size") else ""
    rot = t.get("rotate", 0)
    body = "<br>".join(esc(line) for line in t["text"].split("\n"))
    return (f'<div class="abs {style} halo" style="left:{t["x"]}px;top:{t["y"]}px;{size}'
            f'transform:translate(-50%,-50%) rotate({rot}deg)">{body}</div>')


def build(spec_path: Path):
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    root = spec_path.parent
    base = Image.open(root / spec["base"]).convert("RGB")
    bw, bh = base.size
    # "patches": paint over unwanted marks by cloning a nearby piece of the same terrain,
    # with a soft edge so the seam does not show.
    for pt in spec.get("patches", []):
        x, y, w, h = pt["x"], pt["y"], pt["w"], pt["h"]
        src = base.crop((x + pt["dx"], y + pt["dy"], x + pt["dx"] + w, y + pt["dy"] + h))
        mask = Image.new("L", (w, h), 0)
        f = max(4, min(w, h) // 8)
        mask.paste(255, (f, f, w - f, h - f))
        mask = mask.filter(ImageFilter.GaussianBlur(f / 2))
        base.paste(src, (x, y), mask)
    out_w = spec.get("out_width", 1536)
    scale = out_w / bw
    out_h = round(bh * scale)
    work = Path(spec_path.with_suffix(".work.png"))
    base.resize((out_w, out_h), Image.LANCZOS).save(work)

    sizes = {"w": bw, "h": bh, "numsize": 30, "labsize": 27, "watersize": 30, "regionsize": 34,
             "notesize": 20, "kickersize": 18, "titlesize": 30, "legendsize": 20, "subsize": 19,
             "badge": 30, "badgefont": 18, "sumhead": 18, "sumsize": 15}
    sizes.update(spec.get("sizes", {}))
    parts = [f'<img class="base" src="{work.name}">']
    b = spec.get("banner")
    if b:
        parts.append(f'<div class="abs banner" style="left:{b["x"]}px;top:{b["y"]}px;width:{b["w"]}px;white-space:normal">'
                     f'<div class="k">{esc(b.get("kicker", ""))}</div><div class="t">{esc(b["title"])}</div></div>')
    hd = spec.get("header")
    if hd:
        sub = esc(hd.get("sub", "")).replace(" • ", " <i>•</i> ")
        parts.append(f'<div class="abs hdr" style="left:{hd.get("x", bw // 2)}px;top:{hd["y"]}px;width:{hd["w"]}px">'
                     f'<div class="k">{esc(hd.get("kicker", ""))}</div><div class="t">{esc(hd["title"])}</div>'
                     f'<div class="s">{sub}</div><hr></div>')
    badges = spec.get("badges", False)
    parts += [marker_html(m, sizes, badges) for m in spec.get("markers", [])]
    sm = spec.get("summary")
    if sm:
        parts.append(f'<div class="abs summary" style="left:{sm.get("x", bw // 2)}px;top:{sm["y"]}px;width:{sm["w"]}px">'
                     f'<div class="h">{esc(sm["title"])}</div><div class="b">{esc(sm["text"])}</div></div>')
    parts += [text_html(t) for t in spec.get("texts", [])]
    lg = spec.get("legend")
    if lg:
        items = "".join(f'<div><b>{esc(i + 1)}</b>{esc(s)}</div>' for i, s in enumerate(lg["items"]))
        for k in lg.get("keys", []):
            line = f'border-top:4px {k.get("line", "solid")} {k.get("color", "#a3221b")}'
            items += (f'<div><i style="display:inline-block;width:34px;vertical-align:middle;margin-right:.4em;{line}">'
                      f'</i>{esc(k["text"])}</div>')
        head = f'<div class="h">{esc(lg["title"])}</div>' if lg.get("title") else ""
        parts.append(f'<div class="abs legend{" badges" if badges else ""}" style="left:{lg["x"]}px;top:{lg["y"]}px;'
                     f'grid-template-columns:repeat({lg.get("cols", 2)}, auto)">{head}{items}</div>')

    page = root / (spec_path.stem + ".work.html")
    page.write_text(f'<!doctype html><html lang="th"><head><meta charset="utf-8">'
                    f'<link rel="stylesheet" href="{FONTS}"><style>{CSS % sizes}</style></head>'
                    f'<body>{"".join(parts)}</body></html>', encoding="utf-8")
    out = (root / spec["out"]).resolve()
    with sync_playwright() as pw:
        chrome = pw.chromium.launch(executable_path=CHROME)
        ctx = chrome.new_context(viewport={"width": bw, "height": bh}, device_scale_factor=scale)
        tab = ctx.new_page()
        tab.goto(page.resolve().as_uri())
        tab.evaluate("document.fonts.ready")
        tab.wait_for_timeout(300)
        tab.screenshot(path=str(out), full_page=False)
        chrome.close()
    page.unlink(); work.unlink()
    print(f"{out}  {Image.open(out).size}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("specs", nargs="+", type=Path)
    for s in ap.parse_args().specs:
        build(s.resolve())
