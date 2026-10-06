"""Focused regression tests for review findings (needs network for the Lens part;
`--offline` skips it)."""
import asyncio
import json
import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from pcm.model import TextBlock, TextLine
from pcm.ocr import TOP_TO_BOTTOM, LensOCR, plan_tiles
from pcm.session import _chapter_path
from pcm.translate import untranslated
from pcm.typeset import clean_and_place

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


# ---- offline OCR grouping: a fake Lens returning the raw shapes _ocr_tile reads ----
LTR = 0


def col(text, x, y, c=22, pitch=24.5):
    """Box of a vertical column of `text` whose first glyph's top-left is (x, y)."""
    return text, (x, y, x + c, y + pitch * (len(text) - 1) + c)


def row(text, x, y, c=22, pitch=23.0):
    return text, (x, y, x + pitch * (len(text) - 1) + c, y + c)


class FakeLens:
    """paragraphs: [(writing_direction, [(text, page_box), ...]), ...] in page pixels.
    Glyphs cut by the crop edge are dropped, like Lens does at tile seams."""

    def __init__(self, paragraphs, lang="zh"):
        self.paragraphs, self.lang, self.at, self.calls = paragraphs, lang, (0, 0), 0

    def _clip(self, text, box, vertical, ox, oy, tw, th):
        n = len(text)
        x0, y0, x1, y1 = box
        step = ((y1 - y0) if vertical else (x1 - x0)) / n
        keep = []
        for i, ch in enumerate(text):
            a = (y0 if vertical else x0) + i * step
            g = (x0, a, x1, a + step) if vertical else (a, y0, a + step, y1)
            if g[0] >= ox and g[1] >= oy and g[2] <= ox + tw and g[3] <= oy + th:
                keep.append((ch, g))
        if not keep:
            return None
        bx = (min(g[0] for _, g in keep), min(g[1] for _, g in keep),
              max(g[2] for _, g in keep), max(g[3] for _, g in keep))
        return "".join(ch for ch, _ in keep), bx

    async def process_image(self, tile, **kw):
        self.calls += 1
        ox, oy = self.at
        tw, th = tile.size
        paras = []
        for d, lines in self.paragraphs:
            out = []
            for text, box in lines:
                vert = d == TOP_TO_BOTTOM and box[3] - box[1] >= box[2] - box[0]
                r = self._clip(text, box, vert, ox, oy, tw, th)
                if r is None:
                    continue
                t, (x0, y0, x1, y1) = r
                bb = NS(center_x=((x0 + x1) / 2 - ox) / tw, center_y=((y0 + y1) / 2 - oy) / th,
                        width=(x1 - x0) / tw, height=(y1 - y0) / th, rotation_z=0.0)
                out.append(NS(words=[NS(plain_text=t, text_separator="")], geometry=NS(bounding_box=bb)))
            if out:
                paras.append(NS(writing_direction=d, content_language=self.lang, lines=out))
        return {"raw_response_objects": NS(text=NS(text_layout=NS(paragraphs=paras))),
                "detected_language": self.lang}

    async def aclose(self):
        pass


class FakeOCR(LensOCR):
    async def _ocr_tile(self, api, tile, ox, oy, *a):
        api.at = (ox, oy)   # read synchronously by process_image before its first await
        return await super()._ocr_tile(api, tile, ox, oy, *a)


def fake_read(paragraphs, lang="zh", size=(1080, 1525), img=None, src="auto"):
    ocr = FakeOCR()
    ocr._api = FakeLens(paragraphs, lang)
    img = img if img is not None else Image.new("RGB", size, "white")
    return asyncio.run(ocr.read(img, src, "th")), ocr._api


def texts(blocks):
    return [b.text for b in blocks]


def ocr_vertical_order():
    ok = True
    V = TOP_TO_BOTTOM
    # the user's bubble (page 1080x1525): 搞什么 / 不是准备得 / 很齐全嘛 / 这个闷骚鬼, columns
    # right-to-left, all top-aligned; Lens also read the top row across two columns as "很不"
    real = [col("搞什么", 334, 1140), col("不是准备得", 303, 1140), col("很齐全嘛", 273, 1139),
            col("这个闷骚鬼", 242, 1140)]
    want = "搞什么不是准备得很齐全嘛这个闷骚鬼"
    b, _ = fake_read([(V, real + [row("很不", 273, 1141, pitch=30)])])
    ok &= check("ocr: cross-column read does not scramble/duplicate", texts(b) == [want], f"{texts(b)}")
    ok &= check("ocr: cross-column read is still erased", len(b) == 1 and len(b[0].lines) == 5)
    # only the two inner columns lack the glyph; the outer ones set the columns' top
    b, _ = fake_read([(V, [real[0], col("是准备得", 303, 1164.5), col("齐全嘛", 273, 1163.5), real[3],
                           row("很不", 273, 1141, pitch=30)])])
    ok &= check("ocr: cross-column read supplies glyphs two inner columns lack", texts(b) == [want], f"{texts(b)}")
    # every column under it lacks the glyph: that looks just like a caption touching the columns,
    # so it is kept whole and read first (nothing lost or doubled, still erased)
    b, _ = fake_read([(V, [col("是准备得", 303, 1164.5), col("齐全嘛", 273, 1163.5), row("很不", 273, 1141, pitch=30)])])
    ok &= check("ocr: cross read over columns all lacking the glyph is kept whole",
                texts(b) == ["很不是准备得齐全嘛"] and len(b[0].lines) == 3, f"{texts(b)}")
    for name, extra in (("3 glyphs", row("很不搞", 273, 1141, pitch=30.5)),
                        ("middle row", row("全准", 273, 1188, pitch=30)),
                        ("padded box", ("很不搞", (264, 1132, 365, 1172)))):
        b, _ = fake_read([(V, real + [extra])])
        ok &= check(f"ocr: cross-column read, {name}", texts(b) == [want], f"{texts(b)}")
    b, _ = fake_read([(V, real)])
    ok &= check("ocr: plain four columns", texts(b) == [want], f"{texts(b)}")
    # real horizontal text inside a vertical paragraph stays whole and in place
    b, _ = fake_read([(V, real + [row("真的吗", 250, 1290)])])
    ok &= check("ocr: caption under the columns stays whole", texts(b) == [want + "真的吗"], f"{texts(b)}")
    # a 2-glyph caption whose glyphs sit right over two columns is still no cross-column read
    for lang, cols, cap, x in (("zh", real, "真的", 273),
                               ("ja", [col("今日は", 500, 100), col("晴れだ", 470, 100)], "本当", 470)):
        whole = "".join(t for t, _ in cols)
        y0, y1 = min(t[1][1] for t in cols), max(t[1][3] for t in cols)
        for gap in (3, 12):
            for where, y, w in (("above", y0 - 22 - gap, cap + whole), ("below", y1 + gap, whole + cap)):
                b, _ = fake_read([(V, cols + [row(cap, x, y, pitch=30)])], lang)
                ok &= check(f"ocr: 2-glyph caption {where} the columns stays whole ({lang}, gap {gap})",
                            texts(b) == [w] and len(b[0].lines) == len(cols) + 1, f"{texts(b)}")
        # smaller or off-centre glyphs within half a column of the tops / bottoms
        for c, pitch, dx, gap in ((18, 20, 0, 2), (20, 22, 0, 0), (16, 18, 3, 1), (14, 26, 0, 1)):
            got = [texts(fake_read([(V, cols + [row(cap, x + dx, y, c=c, pitch=pitch)])], lang)[0])
                   for y in (y0 - c - gap, y1 + gap)]
            ok &= check(f"ocr: small 2-glyph caption against the columns stays whole ({lang}, {c}px, gap {gap})",
                        got == [[cap + whole], [whole + cap]], f"{got}")
    # a wide line that is no cross read, in the middle of a single column: read top to bottom
    for x in (360, 380):
        b, _ = fake_read([(V, [col("那么", 400, 100), row("真的吗", x, 160), col("你呢", 400, 196)])])
        ok &= check(f"ocr: wide line mid-column ordered by y (x {x})", texts(b) == ["那么真的吗你呢"], f"{texts(b)}")
    b, _ = fake_read([(V, [col("那么", 400, 100), col("你呢", 400, 196), row("真的", 360, 150, pitch=30),
                           row("好吧", 350, 260, pitch=30)])])
    ok &= check("ocr: wide lines around a single column ordered by y", texts(b) == ["那么真的你呢好吧"], f"{texts(b)}")
    b, _ = fake_read([(V, [col("今日", 500, 100), col("きょう", 524, 100, c=10, pitch=16), row("本当に", 480, 160),
                           col("晴れ", 500, 200)])], "ja")
    ok &= check("ocr: wide line mid-column ordered by y, ruby beside (ja)", texts(b) == ["今日本当に晴れ"], f"{texts(b)}")
    # a small line under a short column, level with the longer ones: read after that column
    for x in (269, 272, 275):
        b, _ = fake_read([(V, real + [row("了吧", x, 1240, c=14, pitch=16)])])
        ok &= check(f"ocr: small line under a short column read after it (x {x})",
                    texts(b) == ["搞什么不是准备得很齐全嘛了吧这个闷骚鬼"], f"{texts(b)}")
    b, _ = fake_read([(V, [col("那么", 400, 100), row("Socks...", 400, 152, c=18, pitch=9)])])
    ok &= check("ocr: Latin word inside a column", texts(b) == ["那么Socks..."], f"{texts(b)}")
    b, _ = fake_read([(V, [col("今日は", 500, 100), col("きょう", 524, 100, c=10, pitch=16), col("晴れ", 470, 100),
                           row("晴今", 470, 101, pitch=30)])], "ja")
    ok &= check("ocr: cross-column read with furigana (ja)", texts(b) == ["今日は晴れ"], f"{texts(b)}")
    # ragged tops and bottoms (centred, staircase, short first column), one paragraph or one per column
    for lang, cols, want in (
            ("zh", [col("你", 520, 230), col("到底想", 490, 205), col("怎样啊!!", 460, 180)], "你到底想怎样啊!!"),
            ("zh", [col("真的", 520, 160), col("可以吗?", 490, 185), col("我", 460, 250)], "真的可以吗?我"),
            ("ja", [col("ねえ", 520, 260), col("今日はいい", 490, 200), col("天気だな", 460, 150)], "ねえ今日はいい天気だな"),
            ("ja", [col("心配するな", 520, 150), col("すぐ", 490, 230), col("戻る", 460, 250)], "心配するなすぐ戻る")):
        b, _ = fake_read([(V, cols)], lang)
        ok &= check(f"ocr: ragged columns, one paragraph ({lang})", texts(b) == [want], f"{texts(b)}")
        b, _ = fake_read([(V, [c]) for c in reversed(cols)], lang)
        ok &= check(f"ocr: ragged columns, paragraph per column ({lang})", texts(b) == [want], f"{texts(b)}")
    # Lens splits one long column into two stacked paragraphs
    for lang, top, bot, nxt in (("zh", "感觉和一开始", "想的完全不一样…", "然后呢"),
                                ("ja", "あの山の向こう", "まで行ってみたいんだ", "それで")):
        split = [(V, [col(top, 500, 100)]), (V, [col(bot, 500, col(top, 500, 100)[1][3] + 4)])]
        b, _ = fake_read(split, lang)
        ok &= check(f"ocr: split long column joined ({lang})", texts(b) == [top + bot], f"{texts(b)}")
        b, _ = fake_read(split + [(V, [col(nxt, 470, 100)])], lang)
        ok &= check(f"ocr: split long column next to another ({lang})", texts(b) == [top + bot + nxt],
                    f"{texts(b)}")
        b, _ = fake_read([(V, [col(top, 500, 100), col(nxt, 470, 100)]), split[1]], lang)
        ok &= check(f"ocr: split column of a multi-column paragraph joined ({lang})",
                    texts(b) == [top + bot + nxt], f"{texts(b)}")
    # a bubble of its own tucked under a short column of a multi-column paragraph is not its tail
    for lang, a, under in (("zh", [col("嗯", 560, 150), col("你到底想怎样", 530, 100), col("说啊", 500, 130)], "我也不知道"),
                           ("ja", [col("ねえ", 560, 100), col("どうしたいんだよ", 530, 100), col("言え", 500, 100)],
                            "わからない")):
        ax0, ay0 = min(t[1][0] for t in a), min(t[1][1] for t in a)
        ax1, ay1 = max(t[1][2] for t in a), max(t[1][3] for t in a)
        for gap in (16, 25):
            for btext, d in ((under, V), (under[0], LTR)):
                bc = col(btext, 562, ay1 + gap)
                img = Image.new("RGB", (800, 600), "white")
                dr = ImageDraw.Draw(img)
                dr.ellipse([ax0 - 18, ay0 - 22, ax1 + 18, ay1 + 22], fill="white", outline="black", width=3)
                dr.ellipse([bc[1][0] - 18, bc[1][1] - 22, bc[1][2] + 18, bc[1][3] + 22], fill="white",
                           outline="black", width=3)
                b, _ = fake_read([(V, a), (d, [bc])], lang, img=img)
                ok &= check(f"ocr: bubble under a short column stays apart ({lang}, {len(btext)} glyphs, gap {gap})",
                            texts(b) == ["".join(t[0] for t in a), btext], f"{texts(b)}")
    # stacked columns with a panel border between them
    img = Image.new("RGB", (800, 400), "white")
    ImageDraw.Draw(img).line([(380, 184), (650, 184)], fill="black", width=3)
    b, _ = fake_read([(V, [col("你好吗", 500, 100)]), (V, [col("我很好", 500, 197)])], "zh", img=img)
    ok &= check("ocr: stacked columns across a border stay apart", texts(b) == ["你好吗", "我很好"], f"{texts(b)}")
    # one-glyph column whose own paragraph Lens calls horizontal
    for lang, cols, want in (("zh", [col("嗯", 520, 200), col("那就拜托", 490, 160), col("你了哦", 460, 160)],
                              "嗯那就拜托你了哦"),
                             ("ja", [col("あ", 520, 200), col("ありがとう", 490, 160), col("また明日ね", 460, 160)],
                              "あありがとうまた明日ね")):
        b, _ = fake_read([(LTR, [cols[0]]), (V, [cols[1]]), (V, [cols[2]])], lang)
        ok &= check(f"ocr: squarish one-glyph column stays in its bubble ({lang})", texts(b) == [want],
                    f"{texts(b)}")
    c1 = col("なんで", 500, 100)    # horizontal "!?" set under a column (tate-chu-yoko)
    b, _ = fake_read([(V, [c1]), (LTR, [row("!?", 500, c1[1][3] + 3, pitch=0)])], "ja")
    ok &= check("ocr: tate-chu-yoko !? joins its column (and gets erased)",
                texts(b) == ["なんで!?"] and len(b[0].lines) == 2, f"{texts(b)}")
    # furigana beside a column is still erased but not read
    b, _ = fake_read([(V, [col("今日", 500, 100), col("きょう", 524, 100, c=10, pitch=16)])], "ja")
    ok &= check("ocr: furigana kept out of the text", texts(b) == ["今日"], f"{texts(b)}")
    # bubbles across a vertical zh page are read right-to-left, like ja
    img = Image.new("RGB", (1080, 800), "white")
    d = ImageDraw.Draw(img)
    d.ellipse([150, 80, 420, 420], outline="black", width=3)
    d.ellipse([600, 80, 870, 420], outline="black", width=3)
    left, right = [col("要做还是", 300, 150), col("不做", 270, 150)], [col("给我说", 750, 150), col("清楚!!!", 720, 150)]
    b, _ = fake_read([(V, left), (V, right)], "zh", img=img)
    ok &= check("ocr: vertical zh page reads bubbles right-to-left", texts(b) == ["给我说清楚!!!", "要做还是不做"],
                f"{texts(b)}")
    return ok


def ocr_user_bubble():
    """The user's bubble with the boxes Lens gave (page 1080x1525): screentone around it,
    outline, glyphs; Lens also read the top row of 很齐全嘛 | 不是准备得 across as "很不"."""
    V = TOP_TO_BOTTOM
    cols = [("搞什么", (334, 1140, 356, 1211)), ("不是准备得", (303, 1140, 325, 1263)),
            ("很齐全嘛", (273, 1139, 295, 1237)), ("这个闷骚鬼", (242, 1140, 264, 1263))]
    cross = ("很不", (273, 1141, 325, 1162))
    want = "搞什么不是准备得很齐全嘛这个闷骚鬼"
    img = Image.new("RGB", (1080, 1525), "white")
    d = ImageDraw.Draw(img)
    for y in range(1040, 1380, 5):
        for x in range(120, 470, 5):
            d.rectangle([x, y, x + 1, y + 1], fill=(110, 110, 110))
    d.ellipse([192, 1100, 372, 1305], fill="white", outline="black", width=3)
    f = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 21)
    for text, (x0, y0, x1, y1) in cols:
        step = (y1 - y0 - 22) / max(len(text) - 1, 1)
        for i, ch in enumerate(text):
            d.text((x0 + 11, y0 + i * step + 11), ch, font=f, fill="black", anchor="mm")
    # what Lens returned later for the same page: the cross read squeezed to 36px (1.5 columns),
    # inside the paragraph of 很齐全嘛
    lens = [(V, [("搞什么", (331, 1134, 355.1, 1212))]), (V, [("不是准备得", (300, 1138, 325, 1263))]),
            (V, [("很不", (278, 1139, 314, 1162)), ("很齐全嘛", (269, 1135, 295.1, 1238))]),
            (V, [("这个闷骚鬼", (240, 1133, 264, 1263))])]
    ok = True
    for name, paras in (("one paragraph", [(V, cols)]),
                        ("one paragraph + cross read", [(V, cols + [cross])]),
                        ("paragraph per column", [(V, [c]) for c in cols]),
                        ("squeezed cross read", lens),
                        ("squeezed cross read, one paragraph", [(V, [ln for _, p in lens for ln in p])])):
        for bg in ("page", "white"):
            b, _ = fake_read(paras, img=img if bg == "page" else None)
            ok &= check(f"ocr: user's bubble, {name} ({bg})", texts(b) == [want], f"{texts(b)}")
    return ok


def ocr_horizontal_unchanged():
    ok = True
    img = Image.new("RGB", (1000, 800), "white")
    d = ImageDraw.Draw(img)
    d.ellipse([60, 60, 460, 360], outline="black", width=3)
    d.ellipse([540, 60, 940, 360], outline="black", width=3)
    for lang, a, b2 in (("ko", ["왜 그렇게", "생각해?"], ["나도 몰라", "그냥 그래"]),
                        ("zh", ["你到底", "想怎样啊"], ["我也不", "知道啊"]),
                        ("en", ["WHY DO YOU", "THINK SO?"], ["I DON'T", "KNOW"])):
        ps = [(LTR, [row(a[0], 150, 150), row(a[1], 160, 180)]), (LTR, [row(b2[0], 630, 150), row(b2[1], 640, 180)])]
        bl, _ = fake_read(ps, lang, img=img)
        sep = "" if lang == "zh" else " "
        want = [sep.join(a), sep.join(b2)]
        ok &= check(f"ocr: horizontal bubbles left-to-right ({lang})", texts(bl) == want, f"{texts(bl)}")
        ok &= check(f"ocr: horizontal stays horizontal ({lang})", all(not x.vertical for x in bl))
    # a big shout line over a small one in the same paragraph
    bl, _ = fake_read([(LTR, [row("야!!", 200, 100, c=44, pitch=46), row("일어나", 200, 150)])], "ko")
    ok &= check("ocr: mixed-size horizontal lines keep order", texts(bl) == ["야!! 일어나"], f"{texts(bl)}")
    return ok


def ocr_tile_seams():
    """Tall strip (tiled with overlap): a 4-column bubble and a long column straddle seams."""
    ok = True
    W, H = 2400, 3400
    img = Image.new("RGB", (W, H), "white")
    ImageDraw.Draw(img).rectangle([0, 0, 12, H], fill="black")     # no blank rows: overlapping tiles
    long_col = "あいうえおかきくけこさしすせそたち"
    bubble = [col("搞什么", 1500, 560, c=44, pitch=47), col("不是准备得", 1440, 560, c=44, pitch=47),
              col("很齐全嘛", 1380, 560, c=44, pitch=47), col("这个闷骚鬼", 1320, 560, c=44, pitch=47)]
    for lang, ps, want in (("ja", [(TOP_TO_BOTTOM, [col(long_col, 1200, 280, c=44, pitch=47)])], [long_col]),
                           ("zh", [(TOP_TO_BOTTOM, bubble)], ["搞什么不是准备得很齐全嘛这个闷骚鬼"])):
        bl, api = fake_read(ps, lang, img=img)
        ok &= check(f"ocr seam: tiled ({lang})", api.calls > 2, f"{api.calls} calls")
        ok &= check(f"ocr seam: one block, nothing lost or doubled ({lang})", texts(bl) == want, f"{texts(bl)}")
    return ok


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
    import poomcatomanga as app
    n = app.App._normalize_url
    ok &= check("url: host:port gets https", n("mangasite.com:8443/read/1") == "https://mangasite.com:8443/read/1")
    ok &= check("url: localhost gets http", n("localhost:8000/x") == "http://localhost:8000/x")
    ok &= check("url: scheme kept", n("http://a.b/c") == "http://a.b/c")
    ok &= check("url: bare host", n("example.com/ch1") == "https://example.com/ch1")
    return ok


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    results = [small_units(), tight_bubble_over_art(), ocr_vertical_order(), ocr_user_bubble(),
               ocr_horizontal_unchanged(), ocr_tile_seams()]
    if "--offline" not in sys.argv:
        results.append(asyncio.run(lens_seam()))
    print("ALL PASS" if all(results) else "SOME FAILED")
