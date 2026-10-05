"""Build a local test 'manga site' exercising the tricky cases real sites use.

Pages are synthetic (simple shapes + original dialogue) so nothing copyrighted is used.
"""
import os
import random

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, "site")
os.makedirs(SITE, exist_ok=True)
JA = "C:/Windows/Fonts/YuGothB.ttc"
KO = "C:/Windows/Fonts/malgunbd.ttf"
ZH = "C:/Windows/Fonts/msyhbd.ttc"


def art(d, W, H, seed):
    rnd = random.Random(seed)
    for _ in range(14):
        x, y = rnd.randint(0, W), rnd.randint(0, H)
        r = rnd.randint(30, 160)
        g = rnd.randint(150, 215)
        d.ellipse([x - r, y - r, x + r, y + r], outline=(g - 90, g - 90, g - 90), width=3, fill=(g, g, g))
    for x in range(0, W, 9):
        d.line([(x, H - 160), (x + 60, H)], fill=(120, 120, 120), width=1)


def vertical_bubble(d, cx, cy, cols, font, size):
    ncol = len(cols)
    longest = max(len(c) for c in cols)
    w = ncol * (size + 14) + 70
    h = longest * (size + 4) + 90
    d.ellipse([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], fill="white", outline="black", width=4)
    x = cx + (ncol - 1) * (size + 14) / 2 - size / 2
    for col in cols:
        y = cy - longest * (size + 4) / 2
        for ch in col:
            if ch in "ー":
                d.text((x + size / 2, y + size / 2), "丨", font=font, fill="black", anchor="mm")
            else:
                d.text((x, y), ch, font=font, fill="black")
            y += size + 4
        x -= size + 14


def horizontal_bubble(d, cx, cy, lines, font, size, rect=False):
    w = max(d.textlength(l, font=font) for l in lines) + 80
    h = len(lines) * (size + 10) + 60
    box = [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]
    if rect:
        d.rounded_rectangle(box, radius=24, fill="white", outline="black", width=4)
    else:
        d.ellipse(box, fill="white", outline="black", width=4)
    y = cy - len(lines) * (size + 10) / 2
    for l in lines:
        d.text((cx, y), l, font=font, fill="black", anchor="ma")
        y += size + 10


def manga_page(name, seed, bubbles):
    W, H = 900, 1300
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    art(d, W, H, seed)
    d.rectangle([20, 20, W - 20, H - 20], outline="black", width=5)
    d.line([(20, 650), (W - 20, 650)], fill="black", width=5)
    f = ImageFont.truetype(JA, 30)
    for cx, cy, cols in bubbles:
        vertical_bubble(d, cx, cy, cols, f, 30)
    img.save(os.path.join(SITE, name), quality=90)


def webtoon(name, lang_font, scenes, W=800, H=5200):
    img = Image.new("RGB", (W, H), (250, 250, 250))
    d = ImageDraw.Draw(img)
    art(d, W, H, 99)
    f = ImageFont.truetype(lang_font, 30)
    for cx, cy, lines in scenes:
        horizontal_bubble(d, cx, cy, lines, f, 30, rect=True)
    img.save(os.path.join(SITE, name), quality=88)


if __name__ == "__main__":
    manga_page("p1.jpg", 1, [
        (650, 260, ["おい、", "待てよ！"]),
        (250, 330, ["どこへ", "行くつもりだ？"]),
        (600, 900, ["あの山の", "向こうまで", "行ってみたいんだ"]),
    ])
    manga_page("p2.png", 2, [
        (640, 300, ["本当に", "大丈夫なの？"]),
        (260, 1000, ["心配するな", "すぐ戻る"]),
    ])
    manga_page("p3.webp", 3, [(450, 400, ["ありがとう", "また明日ね"])])
    manga_page("p4.jpg", 4, [(500, 950, ["今日は", "いい天気だな"])])
    webtoon("strip_ko.jpg", KO, [
        (400, 500, ["오빠, 어디 가?", "나도 같이 갈래!"]),
        (420, 1900, ["위험하니까", "여기서 기다려."]),
        (380, 3300, ["...알았어.", "꼭 돌아와야 해!"]),
        (400, 4700, ["약속할게."]),
    ])
    webtoon("strip_zh.jpg", ZH, [
        (400, 700, ["师兄，你终于来了！"]),
        (400, 2600, ["这里发生了什么事？"]),
        (400, 4300, ["快走，他们追上来了！"]),
    ], H=5000)
    gif = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
    html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>テスト漫画 第1話</title>
<meta http-equiv="Content-Security-Policy" content="img-src 'self' data:">
<style>body{{background:#222;margin:0}} .reader{{width:min(900px,100%);margin:0 auto}} .reader img,.reader canvas{{display:block;width:100%;height:auto;margin:0 auto 8px}}
.logo{{width:40px;height:40px}}</style></head><body>
<img class="logo" src="p4.jpg" style="width:40px;height:40px" alt="logo">
<div class="reader">
<img src="p1.jpg" alt="1">
<img class="lazy" src="{gif}" data-src="p2.png" alt="2">
<picture><source srcset="p3.webp" type="image/webp"><img src="p4.jpg" alt="3"></picture>
<canvas id="cv" width="900" height="1300"></canvas>
<img class="lazy" src="{gif}" data-original="strip_ko.jpg" alt="ko">
<img loading="lazy" src="strip_zh.jpg" alt="zh">
</div>
<script>
const im = new Image(); im.onload = () => document.getElementById('cv').getContext('2d').drawImage(im, 0, 0); im.src = 'p4.jpg';
</script></body></html>"""
    with open(os.path.join(SITE, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)
    # page-flip reader: ONE <img> whose src changes (late results must never land on the wrong page)
    flip = """<!doctype html><html><head><meta charset="utf-8"><title>Flip reader</title>
<style>body{background:#111;margin:0} img{display:block;max-width:900px;width:100%;margin:auto}</style></head>
<body><img id="page" src="p1.jpg"><script>
const pages=['p1.jpg','p2.png','p3.webp'], img=document.getElementById('page'); let i=0;
setInterval(()=>{ if(i<pages.length-1){ i++; img.src=pages[i]; } }, 6000);
</script></body></html>"""
    with open(os.path.join(SITE, "flip.html"), "w", encoding="utf-8") as fh:
        fh.write(flip)
    # chapter jump while work is in flight
    nav = """<!doctype html><html><head><meta charset="utf-8"><title>Chapter A</title></head>
<body style="background:#222"><img src="p1.jpg" style="width:900px"><img src="p2.png" style="width:900px">
<script>setTimeout(()=>{location.href='chapter_b.html'}, 2500)</script></body></html>"""
    with open(os.path.join(SITE, "nav.html"), "w", encoding="utf-8") as fh:
        fh.write(nav)
    chb = """<!doctype html><html><head><meta charset="utf-8"><title>Chapter B</title></head>
<body style="background:#222"><img src="p4.jpg" style="width:900px"><img src="p3.webp" style="width:900px"></body></html>"""
    with open(os.path.join(SITE, "chapter_b.html"), "w", encoding="utf-8") as fh:
        fh.write(chb)
    print("site built at", SITE)
