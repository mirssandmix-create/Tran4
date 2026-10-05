"""End-to-end without a browser: image file -> Lens OCR -> translate -> erase -> typeset.

usage: python tests/test_offline_pipeline.py [image] [engine]
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

from gm.config import Settings
from gm.ocr import LensOCR
from gm.translate import Translator
from gm.typeset import Renderer, clean_and_place

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")


async def main(path, engine):
    s = Settings()
    s.engine = engine
    img = Image.open(path).convert("RGB")
    ocr = LensOCR()
    tr = Translator(s)
    t = time.time()
    blocks = await ocr.read(img, "auto", "th")
    print(f"OCR {time.time() - t:.1f}s, {len(blocks)} blocks")
    for b in blocks:
        print(f"  [{b.id}] {'V' if b.vertical else 'H'} {b.lang} {tuple(round(v) for v in b.box)} {b.text!r}")
    t = time.time()
    used = await tr.translate_page(blocks, "auto", getattr(blocks, "lang", ""), "th")
    print(f"translate ({used}) {time.time() - t:.1f}s")
    for b in blocks:
        print(f"  [{b.id}] -> {b.translation}")
    cleaned, placements = clean_and_place(img, blocks)
    r = Renderer()
    try:
        t = time.time()
        out = await r.render(cleaned, placements, "th")
        print(f"render {time.time() - t:.1f}s")
        t = time.time()
        await r.render(cleaned, placements, "th")
        print(f"render warm {time.time() - t:.2f}s")
    finally:
        await r.close()
    name = os.path.splitext(os.path.basename(path))[0]
    out.save(os.path.join(OUT, f"{name}_translated.png"))
    await ocr.aclose()
    await tr.aclose()


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else os.path.join(OUT, "page_src.png")
    eng = sys.argv[2] if len(sys.argv) > 2 else "google"
    asyncio.run(main(p, eng))

