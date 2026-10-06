"""Smoke test: erase vertical Japanese text in a synthetic bubble and typeset Thai."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw, ImageFont

from pcm.model import TextBlock, TextLine
from pcm.typeset import Renderer, clean_and_place

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT, exist_ok=True)


def make_page():
    W, H = 900, 1200
    img = Image.new("RGB", (W, H), (235, 235, 235))
    d = ImageDraw.Draw(img)
    # some "art": diagonal hatching
    for x in range(-H, W, 14):
        d.line([(x, 0), (x + H, H)], fill=(170, 170, 170), width=2)
    # speech bubble
    d.ellipse([300, 150, 620, 650], fill="white", outline="black", width=4)
    font = ImageFont.truetype("C:/Windows/Fonts/YuGothB.ttc", 34)
    cols = ["なんで", "こんな所に", "いるの？"]
    lines = []
    x = 500
    for col in cols:
        y = 260
        for ch in col:
            d.text((x, y), ch, font=font, fill="black")
            y += 38
        lines.append(TextLine(col, [(x - 2, 256), (x + 38, 256), (x + 38, y + 4), (x - 2, y + 4)]))
        x -= 50
    # caption text on the art (textured background)
    d.text((80, 900), "その時…", font=font, fill="black")
    cap = TextLine("その時…", [(76, 896), (290, 896), (290, 944), (76, 944)])
    b1 = TextBlock("b1", "".join(cols), lines, (398, 256, 538, 645), vertical=True,
                   translation="ทำไมเธอถึงมาอยู่ที่แบบนี้ได้ล่ะ？")
    b2 = TextBlock("b2", "その時…", [cap], (76, 896, 290, 944), vertical=False,
                   translation="ในตอนนั้นเอง…")
    return img, [b1, b2]


def dark(img, box) -> int:
    return sum(img.crop(box).convert("L").histogram()[:100])


async def main():
    img, blocks = make_page()
    img.save(os.path.join(OUT, "page_src.png"))
    t = time.time()
    cleaned, placements = clean_and_place(img, blocks)
    print("clean %.2fs" % (time.time() - t), placements)
    cleaned.save(os.path.join(OUT, "page_clean.png"))
    assert len(placements) == len(blocks), placements
    inner = (400, 258, 536, 600)   # inside the bubble, clear of its outline
    assert dark(cleaned, inner) < dark(img, inner) // 10, "Japanese text not erased"
    r = Renderer()   # every method is a coroutine; the renderer lives on this one event loop
    try:
        t = time.time()
        out = await r.render(cleaned, placements, "th")
        print("render %.2fs" % (time.time() - t))
        t = time.time()
        await r.render(cleaned, placements, "th")
        print("render (warm) %.2fs" % (time.time() - t))
    finally:
        await r.close()
    out.save(os.path.join(OUT, "page_th.png"))
    assert out.size == img.size, (out.size, img.size)
    for p in placements:   # Thai text was drawn in every box
        box = tuple(int(v) for v in p["box"])
        assert dark(out, box) > dark(cleaned, box) + 50, "nothing drawn for " + p["text"]
    print("OK:", os.path.join(OUT, "page_th.png"))


if __name__ == "__main__":
    asyncio.run(main())
