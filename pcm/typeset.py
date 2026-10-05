"""Text removal (numpy) + typesetting (headless Chrome canvas, see render.js).

Pillow on Windows has no Raqm, so it cannot stack Thai vowels/tone marks
correctly. Chrome can, and Intl.Segmenter gives proper Thai word wrapping,
so the final text drawing happens in a small headless Chrome page.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import shutil
import tempfile

import numpy as np
from PIL import Image, ImageDraw

from .config import resource_path
from .model import Box, TextBlock

LOG = logging.getLogger("poomcatomanga.typeset")

# Chrome canvas limit is 32767px per side; keep rendering chunks well below.
MAX_RENDER_CHUNK = 14000


# ---------------------------------------------------------------- erasing ---

def _dilate(mask: np.ndarray, r: int) -> np.ndarray:
    """Square dilation by radius r. Separable running-window 'any' via cumulative sums:
    O(pixels) whatever r is (Pillow's MaxFilter is O(pixels * r^2) and dominated runtime)."""
    if r <= 0:
        return mask
    k = 2 * r + 1
    h, w = mask.shape
    c = np.cumsum(np.pad(mask.astype(np.int32), ((0, 0), (r + 1, r))), axis=1)
    m = (c[:, k:k + w] - c[:, :w]) > 0
    c = np.cumsum(np.pad(m.astype(np.int32), ((r + 1, r), (0, 0))), axis=0)
    return (c[k:k + h, :] - c[:h, :]) > 0


def _blur3(a: np.ndarray) -> np.ndarray:
    p = np.pad(a, ((1, 1), (1, 1), (0, 0)), mode="edge")
    return (p[:-2, :-2] + p[:-2, 1:-1] + p[:-2, 2:] + p[1:-1, :-2] + p[1:-1, 1:-1] +
            p[1:-1, 2:] + p[2:, :-2] + p[2:, 1:-1] + p[2:, 2:]) / 9.0


def inpaint_pull_push(img: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Fill masked pixels smoothly from their surroundings (pull-push pyramid)."""
    I = img.astype(np.float32)
    W = (~mask).astype(np.float32)
    C = I * W[..., None]
    pyr = [(C, W)]
    while min(C.shape[:2]) > 1:
        h, w = C.shape[:2]
        Cp = np.pad(C, ((0, h % 2), (0, w % 2), (0, 0)))
        Wp = np.pad(W, ((0, h % 2), (0, w % 2)))
        C = Cp[0::2, 0::2] + Cp[1::2, 0::2] + Cp[0::2, 1::2] + Cp[1::2, 1::2]
        W = Wp[0::2, 0::2] + Wp[1::2, 0::2] + Wp[0::2, 1::2] + Wp[1::2, 1::2]
        scale = np.minimum(W, 1.0) / np.maximum(W, 1e-6)
        C = C * scale[..., None]
        W = np.minimum(W, 1.0)
        pyr.append((C, W))
    est = pyr[-1][0] / np.maximum(pyr[-1][1], 1e-6)[..., None]
    for C, W in reversed(pyr[:-1]):
        h, w = C.shape[:2]
        up = np.repeat(np.repeat(est, 2, axis=0), 2, axis=1)[:h, :w]
        up = _blur3(up)
        est = C + (1.0 - W)[..., None] * up
    out = I.copy()
    out[mask] = est[mask]
    return np.clip(out, 0, 255).astype(np.uint8)


def _luma(rgb: np.ndarray) -> np.ndarray:
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def _line_thickness(block: TextBlock) -> float:
    vals = []
    for ln in block.lines:
        (x0, y0), (x1, y1), (x2, y2) = ln.poly[0], ln.poly[1], ln.poly[2]
        a = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        b = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        vals.append(min(a, b))
    return float(np.median(vals)) if vals else 12.0


def _flood(sim: np.ndarray, seeds: np.ndarray, max_iter: int = 400) -> np.ndarray:
    """Connected region of `sim` reachable from `seeds` (4-neighbour dilation).
    Works only inside the bounding box of `sim`, which is usually far smaller than the crop."""
    region = seeds & sim
    if not region.any():
        return region
    rows = np.flatnonzero(sim.any(axis=1))
    cols = np.flatnonzero(sim.any(axis=0))
    r0, r1, c0, c1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
    s = sim[r0:r1, c0:c1]
    reg = region[r0:r1, c0:c1].copy()
    for i in range(max_iter):
        g = reg.copy()
        g[1:] |= reg[:-1]
        g[:-1] |= reg[1:]
        g[:, 1:] |= reg[:, :-1]
        g[:, :-1] |= reg[:, 1:]
        g &= s
        if i % 4 == 3 and np.array_equal(g, reg):
            break
        reg = g
    out = np.zeros_like(sim)
    out[r0:r1, c0:c1] = reg
    return out


def _inscribed_rect(region: np.ndarray, center: tuple[float, float]) -> Box | None:
    """Largest axis-aligned rect (over a few aspect ratios) centered near `center` that
    stays inside `region` (>=98% of its pixels)."""
    h, w = region.shape
    if not region.any():
        return None
    ii = np.pad(region.astype(np.int32).cumsum(0).cumsum(1), ((1, 0), (1, 0)))

    def inside_frac(x0, y0, x1, y1):
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(w, int(x1)), min(h, int(y1))
        if x1 <= x0 or y1 <= y0:
            return 0.0
        s = ii[y1, x1] - ii[y0, x1] - ii[y1, x0] + ii[y0, x0]
        return s / float((x1 - x0) * (y1 - y0))

    cx, cy = center
    best, best_area = None, 0.0
    for aspect in (0.25, 0.35, 0.45, 0.55, 0.75, 1.0, 1.3, 1.7, 2.2):
        ra = aspect ** 0.5
        lo, hi = 2.0, float(max(w, h)) * 2
        found = None
        for _ in range(18):
            s = (lo + hi) / 2
            rw, rh = s * ra, s / ra
            r = (cx - rw / 2, cy - rh / 2, cx + rw / 2, cy + rh / 2)
            if r[0] >= 0 and r[1] >= 0 and r[2] <= w and r[3] <= h and inside_frac(*r) >= 0.98:
                found, lo = r, s
            else:
                hi = s
        if found:
            area = (found[2] - found[0]) * (found[3] - found[1])
            if area > best_area:
                best, best_area = found, area
    return best


def clean_and_place(img: Image.Image, blocks: list[TextBlock]) -> tuple[Image.Image, list[dict]]:
    """Erase original text; return cleaned image + render instructions per block."""
    arr = np.asarray(img.convert("RGB")).copy()
    H, W = arr.shape[:2]
    placements: list[dict] = []
    for b in blocks:
        if not (b.translation or "").strip():
            continue  # nothing to write (e.g. watermark the translator dropped): leave it alone
        thick = _line_thickness(b)
        margin = int(max(2, min(10, round(thick * 0.18))))
        bx0, by0, bx1, by1 = b.box
        bx0, by0, bx1, by1 = max(0, bx0), max(0, by0), min(W, bx1), min(H, by1)
        bw, bh = bx1 - bx0, by1 - by0
        if bw < 2 or bh < 2:
            continue
        pad = int(max(24, 0.75 * max(bw, bh)))
        cx0, cy0 = max(0, int(bx0) - pad), max(0, int(by0) - pad)
        cx1, cy1 = min(W, int(bx1) + pad), min(H, int(by1) + pad)
        crop = arr[cy0:cy1, cx0:cx1]
        ch, cw = crop.shape[:2]
        if ch < 4 or cw < 4:
            continue

        m_img = Image.new("L", (cw, ch), 0)
        d = ImageDraw.Draw(m_img)
        for ln in b.lines:
            d.polygon([(x - cx0, y - cy0) for x, y in ln.poly], fill=255)
        base = np.asarray(m_img) > 127
        if not base.any():
            continue
        mask = _dilate(base, margin)
        ring = _dilate(mask, margin + 3) & ~mask
        if not ring.any():
            continue
        c16 = crop.astype(np.int16)
        # Bubble colour: glyph strokes cover well under half of the OCR boxes, so the median
        # *inside* the boxes is the background right behind the text -- robust even when the
        # ring around the text already reaches the art outside a tight bubble.
        if base.sum() >= 40:
            bg0 = np.median(c16[base], axis=0)
        else:
            bg0 = np.median(c16[ring], axis=0)
        # Ink that connects to something outside the text zone = bubble outline / panel
        # border / artwork. It must never be painted over, and it must not count against
        # the "plain bubble background" test either.
        ink = np.abs(c16 - bg0).max(axis=2) > 40
        r_big = int(max(margin + 4, round(thick * 0.45)))
        zone = _dilate(base, r_big)
        zone2 = _dilate(zone, 3)
        protected = _flood(ink & zone2, ink & zone2 & ~zone, max_iter=ch + cw)
        ring_clean = ring & ~_dilate(protected, 1)
        if ring_clean.sum() < 0.3 * ring.sum():
            ring_clean = ring
        ring_px = c16[ring_clean]
        bg = np.median(ring_px, axis=0)
        if np.abs(bg - bg0).max() > 40:      # ring is mostly art: trust the inside of the boxes
            bg = bg0
        dev = np.abs(ring_px - bg).max(axis=1)
        close = float((dev <= 40).mean())
        uniform = close >= 0.80
        flat = uniform and float(np.percentile(dev, 95)) <= 14
        look_for_bubble = close >= 0.55      # only enclosed regions are accepted anyway

        if uniform:
            # OCR boxes are often a few px short of the glyphs (esp. column ends), so clean the
            # whole zone around the text, but only pixels reachable without crossing protected
            # ink (a zone poking past a tight bubble outline never paints the art outside).
            open_zone = zone & ~_dilate(protected, 2)
            fill = base | (mask & ~_dilate(protected, 1)) | _flood(open_zone, mask & open_zone, max_iter=4 * r_big + 20)
            if flat:
                crop[fill] = bg.astype(np.uint8)
            else:  # gradient / light screentone: smooth fill instead of a flat patch
                crop[:] = inpaint_pull_push(crop, fill)
        else:
            crop[:] = inpaint_pull_push(crop, mask)
        arr[cy0:cy1, cx0:cx1] = crop

        lum = float(_luma(bg[None, :].astype(np.float32))[0])
        fg, stroke = ("#000000", "#ffffff") if lum >= 110 else ("#ffffff", "#000000")

        # Where can the translation go?
        tx0, ty0, tx1, ty1 = bx0 - cx0, by0 - cy0, bx1 - cx0, by1 - cy0
        box, region_box = None, None
        if look_for_bubble:
            sim = np.abs(crop.astype(np.int16) - bg).max(axis=2) <= 34
            f = max(1, int(round(max(ch, cw) / 220)))
            hh, ww = (ch // f) * f, (cw // f) * f
            # min-pool so a 2px outline still blocks the flood after downscaling
            sim_s = sim[:hh, :ww].reshape(hh // f, f, ww // f, f).all(axis=(1, 3))
            seeds = np.zeros_like(sim_s)
            seeds[int(ty0 // f):int(ty1 // f) + 1, int(tx0 // f):int(tx1 // f) + 1] = True
            region = _flood(sim_s, seeds, max_iter=2 * max(sim_s.shape))
            if region.any():
                edges = [region[0, :].mean(), region[-1, :].mean(), region[:, 0].mean(), region[:, -1].mean()]
                ys, xs = np.nonzero(region)
                if sum(e > 0.05 for e in edges) <= 1:   # enclosed: a real bubble
                    region_box = (float(xs.min() * f + cx0), float(ys.min() * f + cy0),
                                  float((xs.max() + 1) * f + cx0), float((ys.max() + 1) * f + cy0))
                    centers = [(((tx0 + tx1) / 2) / f, ((ty0 + ty1) / 2) / f), (float(xs.mean()), float(ys.mean()))]
                    rects = [r for r in (_inscribed_rect(region, c) for c in centers) if r]
                    r = max(rects, key=lambda q: (q[2] - q[0]) * (q[3] - q[1])) if rects else None
                    if r:
                        cand = (r[0] * f + cx0, r[1] * f + cy0, r[2] * f + cx0, r[3] * f + cy0)
                        area = (cand[2] - cand[0]) * (cand[3] - cand[1])
                        if area >= 0.45 * bw * bh or (cand[2] - cand[0]) >= 0.6 * (region_box[2] - region_box[0]):
                            box = cand
        if box is None:
            if b.vertical and bh > 1.4 * bw:
                # vertical column(s): make room for horizontal text, but stay inside the bubble
                nw = max(bw, bh * 0.7)
                mx = (bx0 + bx1) / 2
                box = [mx - nw / 2, by0, mx + nw / 2, by1]
            else:
                ex, ey = bw * 0.06, bh * 0.06
                box = [bx0 - ex, by0 - ey, bx1 + ex, by1 + ey]
            lim = region_box or (0, 0, W, H)
            box = (max(lim[0], box[0]), max(lim[1], box[1]), min(lim[2], box[2]), min(lim[3], box[3]))
            if box[2] - box[0] < bw * 0.9 or box[3] - box[1] < bh * 0.9:   # clamp ate it: use the text box
                box = (bx0, by0, bx1, by1)
        placements.append({
            "text": b.translation,
            "box": [round(float(v), 1) for v in box],   # plain floats: numpy ints break json.dumps
            "fg": fg,
            "stroke": stroke,
            "strokeRatio": 0.12 if uniform else 0.2,
            "maxPx": int(max(14, min(80, thick * 1.3))),
        })
    return Image.fromarray(arr), placements


# -------------------------------------------------------------- rendering ---

def _png_data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=False, compress_level=1)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _decode_data_url(data_url: str) -> Image.Image:
    raw = base64.b64decode(data_url.split(",", 1)[1])
    return Image.open(io.BytesIO(raw)).convert("RGB")


class Renderer:
    """Headless Chrome (driven over CDP, no chromedriver) used only to draw text.

    All methods are coroutines and must run on the session's event loop.
    """

    def __init__(self, font: str = "Leelawadee UI", bold: bool = True, min_px: int = 11):
        self.font, self.bold, self.min_px = font, bold, min_px
        self._browser = None
        self._tab = None
        self._profile = None
        self._lock = asyncio.Lock()
        with open(resource_path("pcm/render.js"), "r", encoding="utf-8") as f:
            self._js = f.read()

    async def _ensure(self):
        if self._tab is None:
            from seleniumbase.undetected.cdp_driver import cdp_util
            import mycdp as cdp
            from . import cdp as safe
            LOG.debug("starting text renderer (headless Chrome)")
            if self._profile is None:   # one profile reused across restarts
                self._profile = tempfile.mkdtemp(prefix="pcm_render_")
            self._browser = await asyncio.wait_for(
                cdp_util.start_async(headless=True, user_data_dir=self._profile), 60)
            self._tab = self._browser.main_tab
            try:
                await safe.call(self._tab, cdp.page.navigate("about:blank"), 15)
            except Exception:
                pass
            await safe.evaluate(self._tab, self._js, 15)
        return self._tab

    async def close(self):
        async with self._lock:
            b, self._browser, self._tab = self._browser, None, None
            if b is not None:
                try:
                    b.stop()
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            if self._profile:
                shutil.rmtree(self._profile, ignore_errors=True)
                self._profile = None

    async def _render_chunk(self, img: Image.Image, placements: list[dict], lang: str) -> Image.Image:
        loop = asyncio.get_running_loop()
        data_url = await loop.run_in_executor(None, _png_data_url, img)
        opts = {"lang": lang, "font": self.font, "bold": self.bold, "minPx": self.min_px,
                "maxPx": 80, "format": "image/png"}
        expr = "window.__gmRender(%s, %s, %s)" % (json.dumps(data_url), json.dumps(placements, ensure_ascii=False),
                                                  json.dumps(opts, ensure_ascii=False))
        from . import cdp as safe
        res = None
        async with self._lock:
            for attempt in range(2):
                try:
                    tab = await self._ensure()
                    if not await safe.evaluate(tab, "typeof window.__gmRender === 'function'", 10):
                        await safe.evaluate(tab, self._js, 15)
                    res = await safe.evaluate(tab, expr, 120, await_promise=True)
                    break
                except Exception as e:  # renderer crashed / was closed: restart once
                    LOG.warning("ตัววาดข้อความมีปัญหา (%s) กำลังเริ่มใหม่", e)
                    b, self._browser, self._tab = self._browser, None, None
                    try:
                        b and b.stop()
                    except Exception:
                        pass
                    await asyncio.sleep(0.8)  # let Chrome release the reused profile
        if not res or not res.get("ok"):
            raise RuntimeError("วาดข้อความไม่สำเร็จ: %s" % ((res or {}).get("error") or "no result"))
        return await loop.run_in_executor(None, _decode_data_url, res["dataUrl"])

    async def render(self, img: Image.Image, placements: list[dict], lang: str) -> Image.Image:
        if not placements:
            return img
        W, H = img.size
        if H <= MAX_RENDER_CHUNK:
            return await self._render_chunk(img, placements, lang)
        # Very long webtoon strip: render in horizontal bands cut between text boxes.
        cuts = [0]
        while H - cuts[-1] > MAX_RENDER_CHUNK:
            target = cuts[-1] + MAX_RENDER_CHUNK - 500
            y = target
            for _ in range(200):  # walk up until no box crosses y
                if not any(p["box"][1] < y < p["box"][3] for p in placements):
                    break
                y -= 20
            cuts.append(max(cuts[-1] + 1000, y))
        cuts.append(H)
        out = img.copy()
        for top, bottom in zip(cuts, cuts[1:]):
            part = [dict(p, box=[p["box"][0], p["box"][1] - top, p["box"][2], p["box"][3] - top])
                    for p in placements if top <= (p["box"][1] + p["box"][3]) / 2 < bottom]
            if not part:
                continue
            band = await self._render_chunk(img.crop((0, top, W, bottom)), part, lang)
            out.paste(band, (0, top))
        return out


def encode_jpeg(img: Image.Image, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


JPEG_MAX_SIDE = 65500


def encode_output(img: Image.Image) -> tuple[bytes, str]:
    """JPEG normally; PNG for strips taller than JPEG allows."""
    if max(img.size) > JPEG_MAX_SIDE:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG", compress_level=6)
        return buf.getvalue(), "image/png"
    return encode_jpeg(img, 90), "image/jpeg"
