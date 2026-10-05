"""Probe chrome-lens-py output structure on the synthetic page (and any image given as argv[1])."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image
from chrome_lens_py import LensAPI

HERE = os.path.dirname(os.path.abspath(__file__))


async def main(path):
    img = Image.open(path).convert("RGB")
    async with LensAPI(max_concurrent=2) as api:
        r = await api.process_image(img, output_format="blocks", include_raw_response=True)
    print("keys:", sorted(r.keys()))
    print("detected_language:", r.get("detected_language"))
    raw = r["raw_response_objects"]
    print("raw type:", type(raw).__name__)
    paras = raw.text.text_layout.paragraphs
    print("paragraphs:", len(paras))
    for i, p in enumerate(paras):
        bb = p.geometry.bounding_box
        print(f"P{i} dir={p.writing_direction} lang={p.content_language!r} box=({bb.center_x:.3f},{bb.center_y:.3f},{bb.width:.3f},{bb.height:.3f}) rot={bb.rotation_z:.3f} ctype={bb.coordinate_type}")
        for j, ln in enumerate(p.lines):
            lb = ln.geometry.bounding_box
            text = "".join(w.plain_text + (w.text_separator or "") for w in ln.words)
            print(f"   L{j} {text!r} box=({lb.center_x:.3f},{lb.center_y:.3f},{lb.width:.3f},{lb.height:.3f}) rot={lb.rotation_z:.3f}")
    print("text_blocks[0]:", (r.get("text_blocks") or [None])[0])


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "out", "page_src.png")
    asyncio.run(main(p))
