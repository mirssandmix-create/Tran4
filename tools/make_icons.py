"""Draw the PoomCatoManga icon/logo: a manga speech bubble with cat ears.

Writes icon.ico (16-256 px) and logo.png (256 px) next to the project root.
"""
import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = 1024                     # draw big, downsample for smooth edges
VIOLET, PINK = (139, 92, 246), (236, 72, 153)
INK = (30, 27, 46)


def gradient(size, top, bottom):
    g = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / (size - 1)
        g.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    return g.resize((size, size))


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=230, fill=255)
    img.paste(gradient(S, VIOLET, PINK), (0, 0), mask)

    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)
    # cat ears
    d.polygon([(250, 380), (320, 170), (450, 330)], fill=white)
    d.polygon([(774, 380), (704, 170), (574, 330)], fill=white)
    d.polygon([(290, 340), (325, 235), (395, 320)], fill=PINK + (255,))
    d.polygon([(734, 340), (699, 235), (629, 320)], fill=PINK + (255,))
    # bubble + tail
    d.rounded_rectangle([170, 290, 854, 760], radius=210, fill=white)
    d.polygon([(330, 700), (250, 900), (470, 740)], fill=white)
    # face
    d.ellipse([360, 470, 430, 560], fill=INK)
    d.ellipse([594, 470, 664, 560], fill=INK)
    d.ellipse([380, 482, 404, 506], fill=white)
    d.ellipse([614, 482, 638, 506], fill=white)
    d.arc([452, 560, 516, 624], start=0, end=180, fill=INK, width=18)
    d.arc([508, 560, 572, 624], start=0, end=180, fill=INK, width=18)
    d.ellipse([300, 580, 350, 615], fill=(244, 114, 182, 200))
    d.ellipse([674, 580, 724, 615], fill=(244, 114, 182, 200))
    return img


if __name__ == "__main__":
    big = draw()
    big.resize((256, 256), Image.LANCZOS).save(os.path.join(ROOT, "logo.png"))
    big.resize((256, 256), Image.LANCZOS).save(
        os.path.join(ROOT, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote icon.ico and logo.png")
