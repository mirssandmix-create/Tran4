"""OCR with Google Lens (chrome-lens-py) + speech-bubble grouping.

Lens returns one *paragraph per vertical column* for Japanese/Chinese manga, so
we rebuild bubbles ourselves: merge neighbouring columns/lines of the same
orientation unless a bubble outline separates them, attach furigana to its
column (erased but not translated), and join columns right-to-left.
Tall webtoon strips are cut into tiles so Lens never downscales them (it
shrinks anything over ~1.5 MP / 1600 px); tiles overlap by half so every
normal-sized bubble is seen whole in at least one tile.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import math
import re
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from .model import TextBlock, TextLine, poly_box, union_box

LOG = logging.getLogger("ghostmanga.ocr")

TOP_TO_BOTTOM = 2
LENS_MAX_AREA = 1_500_000
LENS_MAX_SIDE = 1600
LENS_JPEG_QUALITY = 85  # library default is 40, which hurts small text

_KANA_ONLY = re.compile(r"^[぀-ヿー\s・ー]+$")
_HANGUL = re.compile(r"[가-힯ᄀ-ᇿ]")
_KANA_ANY = re.compile(r"[぀-ヿ]")
_HAN = re.compile(r"[㐀-鿿]")
_THAI = re.compile(r"[฀-๿]")
_LATIN = re.compile(r"[A-Za-z]")


def _patch_lens_quality():
    try:
        from chrome_lens_py.core import image_processor as ip
        fn = ip.prepare_image_for_api
        params = list(inspect.signature(fn).parameters)
        if params[-1] == "jpeg_quality" and fn.__defaults__:
            d = list(fn.__defaults__)
            d[-1] = LENS_JPEG_QUALITY
            fn.__defaults__ = tuple(d)
    except Exception as e:  # private API; never fatal
        LOG.debug("lens quality patch skipped: %s", e)


class OCRResult(list):
    """List of TextBlocks plus a flag telling whether some tiles could not be read."""
    partial: bool = False


@dataclass
class _Line:
    text: str
    poly: list
    box: tuple
    vertical: bool

    @property
    def thickness(self) -> float:  # character size estimate
        w, h = self.box[2] - self.box[0], self.box[3] - self.box[1]
        return w if self.vertical else h


@dataclass
class _Group:
    lines: list = field(default_factory=list)
    vertical: bool = False
    lang: str = ""
    cut: bool = False            # touches an interior tile edge (may be truncated)
    furigana: list = field(default_factory=list)  # erased, not translated
    _box: tuple | None = None
    _char: float | None = None

    def invalidate(self):
        self._box = self._char = None

    @property
    def box(self):
        if self._box is None:
            self._box = union_box([ln.box for ln in self.lines])
        return self._box

    @property
    def char(self) -> float:
        if self._char is None:
            self._char = float(np.median([ln.thickness for ln in self.lines])) if self.lines else 10.0
        return self._char


def _rotated_poly(cx, cy, w, h, rot):
    c, s = math.cos(rot), math.sin(rot)
    return [(cx + dx * c - dy * s, cy + dx * s + dy * c)
            for dx, dy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2))]


def _geom_to_px(bb, tw, th, oy, ox=0):
    cx, cy, w, h = bb.center_x * tw + ox, bb.center_y * th + oy, bb.width * tw, bb.height * th
    poly = _rotated_poly(cx, cy, w, h, getattr(bb, "rotation_z", 0.0) or 0.0)
    return poly, poly_box(poly)


def plan_tiles(img: Image.Image) -> list[tuple[int, int]]:
    """Row ranges to OCR separately. Prefers cuts on blank rows (no overlap needed);
    otherwise tiles overlap by half a tile."""
    W, H = img.size
    scale = 1.0
    if W * H > LENS_MAX_AREA and (W > LENS_MAX_SIDE or H > LENS_MAX_SIDE):
        scale = min(LENS_MAX_SIDE / W, LENS_MAX_SIDE / H)
    if scale >= 0.6:   # normal manga pages: one request (Lens reads them fine slightly shrunk)
        return [(0, H)]
    tile_h = max(LENS_MAX_SIDE if W <= LENS_MAX_SIDE else 0, LENS_MAX_AREA // max(W, 1))
    tile_h = max(400, min(tile_h, LENS_MAX_SIDE))
    g = np.asarray(img.convert("L"), dtype=np.int16)
    blank = (g.max(axis=1) - g.min(axis=1)) < 14
    tiles, y = [], 0
    while y < H:
        end = min(y + tile_h, H)
        if end >= H:
            tiles.append((y, H))
            break
        cand = np.flatnonzero(blank[y + tile_h // 2:end])
        if cand.size:                       # clean cut through empty space
            cut = y + tile_h // 2 + int(cand[-1])
            tiles.append((y, cut))
            y = cut
        else:                               # no gap: overlap by half a tile
            tiles.append((y, end))
            y = end - tile_h // 2
    return tiles


def _script_lang(text: str, hint: str) -> str:
    if _HANGUL.search(text):
        return "ko"
    if _KANA_ANY.search(text):
        return "ja"
    if _HAN.search(text):
        return "ja" if hint == "ja" else "zh"
    if _THAI.search(text):
        return "th"
    if _LATIN.search(text):
        return "en"
    return hint or ""


def _flood(sim: np.ndarray, seeds: np.ndarray, max_iter: int = 400) -> np.ndarray:
    region = seeds & sim
    for _ in range(max_iter):
        p = np.pad(region, 1)
        grown = (p[1:-1, 1:-1] | p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]) & sim
        if (grown == region).all():
            break
        region = grown
    return region


def _separated(gray: np.ndarray, a: _Group, b: _Group, vertical: bool) -> bool:
    """True if a connected line of ink (bubble outline, panel border) runs between the
    two groups -- straight or curved."""
    ax0, ay0, ax1, ay1 = a.box
    bx0, by0, bx1, by1 = b.box
    H, W = gray.shape
    if vertical:   # side by side: strip between them, a little taller than their overlap
        x0, x1 = int(min(ax1, bx1)), int(math.ceil(max(ax0, bx0)))
        y0, y1 = int(max(ay0, by0)) - 4, int(min(ay1, by1)) + 4
    else:          # stacked
        y0, y1 = int(min(ay1, by1)), int(math.ceil(max(ay0, by0)))
        x0, x1 = int(max(ax0, bx0)) - 4, int(min(ax1, bx1)) + 4
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if x1 - x0 < 2 or y1 - y0 < 4:
        return False
    strip = gray[y0:y1, x0:x1]
    bg = np.median(strip)
    ink = np.abs(strip - bg) > 70
    if not ink.any():
        return False
    h, w = ink.shape
    if vertical:   # does one ink component reach from the top band to the bottom band?
        k = max(1, int(h * 0.2))
        start, end = ink[:k, :], ink[-k:, :]
        seeds = np.zeros_like(ink); seeds[:k, :] = start
        reach = _flood(ink, seeds, max_iter=h + w)
        return bool((reach[-k:, :] & end).any())
    k = max(1, int(w * 0.2))
    seeds = np.zeros_like(ink); seeds[:, :k] = ink[:, :k]
    reach = _flood(ink, seeds, max_iter=h + w)
    return bool((reach[:, -k:] & ink[:, -k:]).any())


def _should_merge(a: _Group, b: _Group, gray: np.ndarray) -> bool:
    if a.vertical != b.vertical:
        return False
    ax0, ay0, ax1, ay1 = a.box
    bx0, by0, bx1, by1 = b.box
    ca, cb = a.char, b.char
    c = (ca + cb) / 2
    # cheap geometric rejects first
    if a.vertical:
        gap = max(bx0 - ax1, ax0 - bx1)
        if gap > 1.25 * c:
            return False
        ov = min(ay1, by1) - max(ay0, by0)
        short = min(ay1 - ay0, by1 - by0)
        if ov < 0.25 * short and abs(ay0 - by0) > 1.5 * c:
            return False
    else:
        gap = max(by0 - ay1, ay0 - by1)
        if gap > 1.0 * c:
            return False
        ov = min(ax1, bx1) - max(ax0, bx0)
        short = min(ax1 - ax0, bx1 - bx0)
        centered = abs((ax0 + ax1) / 2 - (bx0 + bx1) / 2) < 0.6 * max(ax1 - ax0, bx1 - bx0)
        if ov < 0.2 * short and not centered:
            return False
    if not (0.6 <= ca / max(cb, 1e-3) <= 1.67):
        return False
    return not _separated(gray, a, b, a.vertical)


def _attach_furigana(groups: list[_Group]) -> list[_Group]:
    """Small kana-only vertical paragraphs right next to a bigger column are ruby text."""
    main = [g for g in groups if g.vertical]
    out = []
    for g in groups:
        text = "".join(l.text for l in g.lines)
        if g.vertical and _KANA_ONLY.match(text or "") and len(text) <= 10:
            gx0, gy0, gx1, gy1 = g.box
            host = None
            for m in main:
                if m is g or m.char < g.char / 0.6:
                    continue
                mx0, my0, mx1, my1 = m.box
                gap = max(mx0 - gx1, gx0 - mx1)
                yov = min(gy1, my1) - max(gy0, my0)
                if gap < 0.5 * m.char and yov > 0.5 * (gy1 - gy0):
                    host = m
                    break
            if host is not None:
                host.furigana.extend(g.lines)
                continue
        out.append(g)
    return out


def group_lines(paragraphs: list[_Group], gray: np.ndarray) -> list[_Group]:
    """Merge paragraphs into bubbles (union-find over nearby pairs only)."""
    groups = _attach_furigana([g for g in paragraphs if g.lines])
    n = len(groups)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    order = sorted(range(n), key=lambda i: groups[i].box[1])
    for oi, i in enumerate(order):
        gi = groups[i]
        reach = gi.box[3] + 2.0 * gi.char
        for j in order[oi + 1:]:
            gj = groups[j]
            if gj.box[1] > reach:
                break
            ri, rj = find(i), find(j)
            if ri == rj:
                continue
            if _should_merge(gi, gj, gray):
                parent[rj] = ri
    merged: dict[int, _Group] = {}
    for i in range(n):          # every group is added exactly once to its root's bucket
        r = find(i)
        g = groups[i]
        m = merged.get(r)
        if m is None:
            merged[r] = _Group(lines=list(g.lines), vertical=g.vertical, lang=g.lang, cut=g.cut,
                               furigana=list(g.furigana))
        else:
            m.lines += g.lines
            m.furigana += g.furigana
            m.lang = m.lang or g.lang
            m.cut = m.cut or g.cut
            m.invalidate()
    return list(merged.values())


def _cluster(lines: list[_Line], vertical: bool) -> list[list[_Line]]:
    """Lines that share a column (vertical) / row (horizontal) by >50% overlap."""
    a0, a1 = (0, 2) if vertical else (1, 3)
    clusters: list[list[_Line]] = []
    for ln in sorted(lines, key=lambda l: (l.box[a0] + l.box[a1]) / 2):
        lo, hi = ln.box[a0], ln.box[a1]
        for cl in clusters:
            clo = min(l.box[a0] for l in cl)
            chi = max(l.box[a1] for l in cl)
            ov = min(hi, chi) - max(lo, clo)
            if ov > 0.5 * min(hi - lo, chi - clo):
                cl.append(ln)
                break
        else:
            clusters.append([ln])
    return clusters


def _compose(g: _Group, hint: str) -> tuple[str, list]:
    """Group text in reading order (furigana excluded). Returns (text, all lines to erase)."""
    if g.vertical:
        cols = _cluster(g.lines, True)
        cols.sort(key=lambda cl: -np.mean([(l.box[0] + l.box[2]) / 2 for l in cl]))
        ordered = [l for cl in cols for l in sorted(cl, key=lambda l: l.box[1])]
        if len(ordered) >= 2:   # ruby columns Lens kept inside the paragraph
            base = [l.thickness for l in ordered if not _KANA_ONLY.match(l.text or "")] or [l.thickness for l in ordered]
            ref = float(np.median(base))
            text_lines = [l for l in ordered
                          if not (l.thickness < 0.6 * ref and _KANA_ONLY.match(l.text or "") and len(l.text) <= 8)]
        else:
            text_lines = ordered
    else:
        rows = _cluster(g.lines, False)
        rows.sort(key=lambda cl: np.mean([(l.box[1] + l.box[3]) / 2 for l in cl]))
        ordered = [l for cl in rows for l in sorted(cl, key=lambda l: l.box[0])]
        text_lines = ordered
    lang = g.lang or _script_lang("".join(ln.text for ln in text_lines), hint)
    sep = "" if lang in ("ja", "zh") else " "
    text = sep.join(ln.text.strip() for ln in text_lines if ln.text.strip())
    if lang in ("ja", "zh"):
        text = re.sub(r"\s+", "", text)
    return text, ordered + list(g.furigana)


def _is_noise(text: str, src_hint: str, target: str) -> bool:
    t = text.strip()
    if not t or not any(ch.isalpha() for ch in t):
        return True
    if target.startswith("th") and len(_THAI.findall(t)) > len(t) * 0.5:
        return True  # already Thai
    if _LATIN.search(t) and not (_HAN.search(t) or _KANA_ANY.search(t) or _HANGUL.search(t)):
        # Latin-only text on a CJK page is usually a watermark / site name
        if src_hint in ("ja", "zh", "ko") or re.search(r"(https?://|www\.|\.com|\.net|\.org)", t, re.I):
            return True
        if len(t) <= 1:
            return True
    return False


def _reading_order(blocks: list[TextBlock], lang: str) -> list[TextBlock]:
    """Rows of bubbles by vertical overlap; right-to-left inside a row for manga."""
    if not blocks:
        return blocks
    rows: list[list[TextBlock]] = []
    for b in sorted(blocks, key=lambda b: b.box[1]):
        if rows:
            row = rows[-1]
            mid = float(np.mean([(r.box[1] + r.box[3]) / 2 for r in row]))
            if b.box[1] < mid:
                row.append(b)
                continue
        rows.append([b])
    rtl = lang == "ja"
    out = []
    for row in rows:
        out += sorted(row, key=lambda b: -(b.box[0] + b.box[2]) / 2 if rtl else b.box[0])
    return out


def _overlap_frac(a, b) -> float:
    ix = min(a[2], b[2]) - max(a[0], b[0])
    iy = min(a[3], b[3]) - max(a[1], b[1])
    if ix <= 0 or iy <= 0:
        return 0.0
    area = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return ix * iy / max(area, 1e-6)


def _covers(k, g, tol: float) -> bool:
    """Does kept block k contain cut fragment g along both axes (within tol px)?"""
    return (k[0] <= g[0] + tol and k[2] >= g[2] - tol and k[1] <= g[1] + tol and k[3] >= g[3] - tol)


def _dedupe_tiles(groups: list[_Group]) -> tuple[list[_Group], list[_Group]]:
    """Paragraphs seen in two overlapping tiles: keep whole copies once. Truncated copies
    are dropped only when a whole copy really contains them; the rest (blocks longer than
    the overlap) are returned separately so they can be re-read from a focused crop."""
    whole = [g for g in groups if not g.cut]
    cut = [g for g in groups if g.cut]
    kept: list[_Group] = []
    for g in whole:
        if not any(g.vertical == k.vertical and _overlap_frac(g.box, k.box) > 0.5 for k in kept):
            kept.append(g)
    uncovered = [g for g in cut if not any(_covers(k.box, g.box, max(4.0, 0.6 * g.char)) for k in kept)]
    return kept, uncovered


def _clusters(groups: list[_Group]) -> list[tuple[float, float, float, float]]:
    """Union boxes of fragments that belong together (overlapping or touching)."""
    boxes = [list(g.box) for g in groups]
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if a[0] <= b[2] + 20 and b[0] <= a[2] + 20 and a[1] <= b[3] + 20 and b[1] <= a[3] + 20:
                    boxes[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    return [tuple(b) for b in boxes]


class LensOCR:
    def __init__(self):
        self._api = None
        self._lock = asyncio.Lock()
        _patch_lens_quality()

    async def _get_api(self):
        async with self._lock:
            if self._api is None:
                from chrome_lens_py import LensAPI
                self._api = LensAPI(max_concurrent=3, timeout=60)
            return self._api

    async def aclose(self):
        if self._api is not None:
            try:
                await self._api.aclose()
            except Exception:
                pass
            self._api = None

    async def _ocr_tile(self, api, tile: Image.Image, ox: int, oy: int, top_inner: bool, bottom_inner: bool,
                        hint: str):
        """OCR one crop placed at (ox, oy). `top_inner/bottom_inner`: that edge cuts through
        the page, so paragraphs touching it may be truncated (marked `cut`)."""
        lang_arg = hint if hint in ("ja", "ko", "zh", "en") else None
        last = None
        for attempt in range(3):
            try:
                r = await api.process_image(tile, ocr_language=lang_arg, output_format="blocks",
                                            include_raw_response=True)
                break
            except Exception as e:  # network / server hiccup
                last = e
                if attempt < 2:
                    await asyncio.sleep(3.0 * (attempt + 1) if "429" in str(e) else 1.5 * (attempt + 1))
        else:
            raise RuntimeError(f"Google Lens อ่านรูปไม่ได้: {last}")
        raw = r.get("raw_response_objects")
        page_lang = (r.get("detected_language") or "").split("-")[0]
        out = []
        if raw is None:
            return out, page_lang
        tw, th = tile.size
        for p in raw.text.text_layout.paragraphs:
            lines = []
            for ln in p.lines:
                text = "".join(w.plain_text + (w.text_separator or "") for w in ln.words).strip()
                if not text:
                    continue
                poly, box = _geom_to_px(ln.geometry.bounding_box, tw, th, oy, ox)
                w, h = box[2] - box[0], box[3] - box[1]
                vertical = p.writing_direction == TOP_TO_BOTTOM or (h > 1.6 * w and len(text) >= 2)
                lines.append(_Line(text, poly, box, vertical))
            if not lines:
                continue
            vertical = sum(l.vertical for l in lines) * 2 >= len(lines)
            for l in lines:
                l.vertical = vertical
            g = _Group(lines=lines, vertical=vertical, lang=(p.content_language or "").split("-")[0])
            gy0, gy1 = g.box[1], g.box[3]
            tol = max(3.0, 1.2 * g.char)   # Lens often drops the half-cut glyph at a tile edge
            g.cut = (top_inner and gy0 <= oy + tol) or (bottom_inner and gy1 >= oy + th - tol)
            out.append(g)
        return out, page_lang

    async def _repair(self, api, img: Image.Image, frags: list[_Group], hint: str) -> list[_Group]:
        """Re-read blocks that were truncated in every tile from a crop around them."""
        W, H = img.size
        fixed: list[_Group] = []
        for cb in _clusters(frags)[:6]:
            char = max(g.char for g in frags)
            m = int(2 * char + 30)
            x0, y0 = max(0, int(cb[0]) - m), max(0, int(cb[1]) - m)
            x1, y1 = min(W, int(cb[2]) + m), min(H, int(cb[3]) + m)
            try:
                groups, _ = await self._ocr_tile(api, img.crop((x0, y0, x1, y1)), x0, y0, False, False, hint)
            except Exception as e:
                LOG.debug("repair crop failed: %s", e)
                continue
            for g in groups:
                bx0, by0, bx1, by1 = g.box
                inside = (bx0 > x0 + 2 or x0 == 0) and (bx1 < x1 - 2 or x1 == W) and \
                         (by0 > y0 + 2 or y0 == 0) and (by1 < y1 - 2 or y1 == H)
                if inside and _overlap_frac(g.box, cb) > 0.3:
                    fixed.append(g)
        return fixed

    async def read(self, img: Image.Image, src_lang: str, target: str) -> OCRResult:
        api = await self._get_api()
        img = img.convert("RGB")
        W, H = img.size
        hint = "" if src_lang == "auto" else src_lang
        paras: list[_Group] = []
        langs = []
        tiles = plan_tiles(img)
        failed = 0

        async def one(y0, y1):
            tile = img if (y0 == 0 and y1 == H) else img.crop((0, y0, W, y1))
            return await self._ocr_tile(api, tile, 0, y0, y0 > 0, y1 < H, hint)

        # tiles in parallel (the Lens client itself caps concurrent requests)
        results = await asyncio.gather(*(one(y0, y1) for y0, y1 in tiles), return_exceptions=True)
        for (y0, y1), res in zip(tiles, results):
            if isinstance(res, BaseException):
                failed += 1
                LOG.warning("อ่านตัวหนังสือบางส่วนของรูปไม่ได้ (%d-%dpx): %s", y0, y1, res)
                continue
            groups, page_lang = res
            paras += groups
            if page_lang:
                langs.append(page_lang)
        if failed == len(tiles):
            raise RuntimeError("Google Lens อ่านรูปนี้ไม่ได้ (ลองกดแปลรูปที่พลาดใหม่ภายหลัง)")
        if len(tiles) > 1:
            kept, uncovered = _dedupe_tiles(paras)
            if uncovered:
                repaired = await self._repair(api, img, uncovered, hint)
                if repaired:
                    kept = [k for k in kept if not any(_overlap_frac(k.box, r.box) > 0.4 for r in repaired)]
                    kept += repaired
                    uncovered = [u for u in uncovered if not any(_overlap_frac(u.box, r.box) > 0.4 for r in repaired)]
                # whatever could not be repaired: keep the biggest non-overlapping fragments
                for g in sorted(uncovered, key=lambda g: -(g.box[2] - g.box[0]) * (g.box[3] - g.box[1])):
                    if not any(_overlap_frac(g.box, k.box) > 0.4 for k in kept):
                        kept.append(g)
            paras = kept
        if not hint and langs:
            hint = max(set(langs), key=langs.count)
        gray = np.asarray(img.convert("L"), dtype=np.int16)
        loop = asyncio.get_running_loop()
        groups = await loop.run_in_executor(None, group_lines, paras, gray)
        blocks = []
        for g in groups:
            text, erase_lines = _compose(g, hint)
            if _is_noise(text, hint, target):
                continue
            box = g.box
            if box[3] - box[1] < 6 or box[2] - box[0] < 6:
                continue
            blocks.append(TextBlock(id="", text=text, vertical=g.vertical,
                                    lines=[TextLine(ln.text, ln.poly) for ln in erase_lines],
                                    box=union_box([ln.box for ln in erase_lines]),
                                    lang=g.lang or _script_lang(text, hint)))
        langs_b = [b.lang for b in blocks if b.lang]
        page_lang = hint or (max(set(langs_b), key=langs_b.count) if langs_b else "")
        result = OCRResult(_reading_order(blocks, page_lang))
        for i, b in enumerate(result, 1):
            b.id = str(i)
        result.partial = failed > 0
        result.lang = page_lang
        return result
