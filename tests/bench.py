"""Time every pipeline stage on realistic page sizes (synthetic content)."""
import asyncio
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw, ImageFont

from gm.config import Settings
from gm.ocr import LensOCR, plan_tiles
from gm.translate import Translator
from gm.typeset import Renderer, clean_and_place, encode_output

JA = "C:/Windows/Fonts/YuGothB.ttc"
KO = "C:/Windows/Fonts/malgunbd.ttf"
LINES_JA = ["おい待てよ", "どこへ行く", "心配するな", "すぐ戻るから", "本当なの", "ありがとう", "また明日ね", "早く来い"]
LINES_KO = ["어디 가?", "나도 갈래!", "위험해.", "기다려.", "알았어.", "꼭 와야 해!", "약속할게.", "고마워."]


def hires_page():
    W, H = 1800, 2560
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    rnd = random.Random(1)
    for x in range(-H, W, 7):                       # screentone-ish background
        d.line([(x, 0), (x + H, H)], fill=(200, 200, 200), width=1)
    f = ImageFont.truetype(JA, 52)
    for i in range(8):
        cx, cy = 250 + (i % 3) * 600 + rnd.randint(-40, 40), 300 + (i // 3) * 800 + rnd.randint(-40, 40)
        cols = [LINES_JA[i][:3], LINES_JA[i][3:]] if len(LINES_JA[i]) > 3 else [LINES_JA[i]]
        w, h = len(cols) * 70 + 90, max(len(c) for c in cols) * 58 + 110
        d.ellipse([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], fill="white", outline="black", width=5)
        x = cx + (len(cols) - 1) * 35 - 26
        for col in cols:
            y = cy - len(col) * 29
            for ch in col:
                d.text((x, y), ch, font=f, fill="black")
                y += 58
            x -= 70
    return img


def strip():
    W, H = 800, 12000
    img = Image.new("RGB", (W, H), (245, 245, 245))
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(KO, 30)
    for i in range(10):
        cy = 600 + i * 1150
        t = LINES_KO[i % len(LINES_KO)]
        w = d.textlength(t, font=f) + 80
        d.rounded_rectangle([400 - w / 2, cy - 45, 400 + w / 2, cy + 45], radius=20, fill="white", outline="black", width=4)
        d.text((400, cy), t, font=f, fill="black", anchor="mm")
    return img


async def run(name, img, ocr, tr, rend):
    t0 = time.time()
    blocks = await ocr.read(img, "auto", "th")
    t1 = time.time()
    await tr.translate_page(blocks, "auto", getattr(blocks, "lang", ""), "th")
    t2 = time.time()
    cleaned, placements = clean_and_place(img, blocks)
    t3 = time.time()
    out = await rend.render(cleaned, placements, "th")
    t4 = time.time()
    data, _ = encode_output(out)
    t5 = time.time()
    print(f"{name:7s} {img.size[0]}x{img.size[1]} tiles={len(plan_tiles(img))} blocks={len(blocks)} | "
          f"ocr {t1-t0:.1f}s  translate {t2-t1:.1f}s  erase {t3-t2:.1f}s  render {t4-t3:.1f}s  "
          f"encode {t5-t4:.1f}s  TOTAL {t5-t0:.1f}s", flush=True)


async def main():
    ocr, tr, rend = LensOCR(), Translator(Settings()), Renderer()
    try:
        await rend._ensure()
        for name, img in (("hires", hires_page()), ("strip", strip())):
            await run(name, img, ocr, tr, rend)
    finally:
        await rend.close()
        await ocr.aclose()
        await tr.aclose()


if __name__ == "__main__":
    asyncio.run(main())
