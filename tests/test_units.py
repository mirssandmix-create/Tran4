"""Focused regression tests for review findings (needs network for the Lens part)."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from gm.model import TextBlock, TextLine
from gm.ocr import LensOCR, plan_tiles
from gm.session import _chapter_path
from gm.translate import untranslated
from gm.typeset import clean_and_place

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
JA = "C:/Windows/Fonts/YuGothB.ttc"


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    return cond


def seam_page():
    """2400px wide (tiles of 625px), no blank rows (dark border), one 17-char column over seams."""
    W, H = 2400, 3400
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 12, H], fill="black")              # no blank rows anywhere
    f = ImageFont.truetype(JA, 44)
    col = "あいうえおかきくけこさしすせそたち"
    x, y = 1200, 280
    for ch in col:
        d.text((x, y), ch, font=f, fill="black")
        y += 47
    d.text((400, 2000), "こんにちは", font=f, fill="black")
    return img, col


async def lens_seam():
    img, col = seam_page()
    tiles = plan_tiles(img)
    ocr = LensOCR()
    try:
        blocks = await ocr.read(img, "auto", "th")
    finally:
        await ocr.aclose()
    texts = [b.text for b in blocks]
    joined = "".join(texts)
    ok = check("seam: tiles planned", len(tiles) > 2, f"{len(tiles)} tiles")
    hit = [t for t in texts if "あいう" in t or "たち" in t]
    ok &= check("seam: long column comes back as ONE block", len(hit) == 1, f"{texts}")
    ok &= check("seam: no characters lost or duplicated", hit and hit[0] == col, f"{hit}")
    ok &= check("seam: other text still found", "こんにちは" in joined)
    return ok


def tight_bubble_over_art():
    """52px column in a narrow bubble over hatching: placement must stay inside the bubble."""
    W, H = 600, 700
    img = Image.new("RGB", (W, H), (150, 150, 150))
    d = ImageDraw.Draw(img)
    for x in range(-H, W, 8):
        d.line([(x, 0), (x + H, H)], fill=(90, 90, 90), width=2)
    d.ellipse([224, 150, 306, 560], fill="white", outline="black", width=3)
    f = ImageFont.truetype(JA, 52)
    y = 260
    for ch in "なに":
        d.text((239, y), ch, font=f, fill="black")
        y += 58
    line = TextLine("なに", [(237, 258), (291, 258), (291, 378), (237, 378)])
    b = TextBlock("1", "なに", [line], (237, 258, 291, 378), vertical=True, translation="อะไรนะ")
    _, placements = clean_and_place(img, [b])
    box = placements[0]["box"]
    json.dumps(placements)   # must be serialisable (numpy ints used to break this)
    return check("tight bubble: text box inside bubble x-range", box[0] >= 222 and box[2] <= 308, f"{box}")


def small_units():
    ok = True
    ok &= check("untranslated ignores punctuation-only", not untranslated("……", "……", "th"))
    ok &= check("untranslated ignores ・・・", not untranslated("えっ・・・", "เอ๊ะ・・・", "th"))
    ok &= check("untranslated catches kana echo", untranslated("なんで", "なんで", "th"))
    ok &= check("untranslated catches hangul left", untranslated("왜?", "왜?", "th"))
    ok &= check("chapter path ignores page numbers",
                _chapter_path("/chapter/abc/3") == _chapter_path("/chapter/abc/12") == "/chapter/abc")
    ok &= check("chapter path ignores query", _chapter_path("/read/ch-5?page=2") == _chapter_path("/read/ch-5?page=9"))
    ok &= check("chapter path keeps chapter change", _chapter_path("/manga/x/chapter-5") != _chapter_path("/manga/x/chapter-6"))
    import ghostmanga5 as app
    n = app.App._normalize_url
    ok &= check("url: host:port gets https", n("mangasite.com:8443/read/1") == "https://mangasite.com:8443/read/1")
    ok &= check("url: localhost gets http", n("localhost:8000/x") == "http://localhost:8000/x")
    ok &= check("url: scheme kept", n("http://a.b/c") == "http://a.b/c")
    ok &= check("url: bare host", n("example.com/ch1") == "https://example.com/ch1")
    return ok


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    results = [small_units(), tight_bubble_over_art(), asyncio.run(lens_seam())]
    print("ALL PASS" if all(results) else "SOME FAILED")
