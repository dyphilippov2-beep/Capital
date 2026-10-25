"""Рисует искорки для логотипа: shorts/logo_sparkles.mov (прозрачный фон, петля 3 с).

Запускать после замены logo.png: python shorts/make_sparkles.py (нужен Pillow).
Рамка = логотип шириной LOGO_W плюс поле PAD со всех сторон, render() кладёт её поверх логотипа.
"""
import math
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
LOGO_W, PAD, FPS, FRAMES = 260, 30, 30, 90
# (x, y) в долях логотипа, начало вспышки и длительность в секундах, размер
SPARKS = [(0.51, 0.17, 0.10, 0.55, 1.0), (0.20, 0.36, 0.95, 0.5, 0.8),
          (0.86, 0.62, 1.75, 0.5, 0.9), (0.33, 0.21, 2.35, 0.45, 0.7),
          (0.62, 0.92, 1.30, 0.5, 0.85)]


def star(size, angle):
    """Четырёхлучевая искорка с ореолом (рисуется в 4 раза крупнее и уменьшается)."""
    side = 8 * size + 8
    im = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    draw, c, length = ImageDraw.Draw(im), side / 2, size * 4

    def ray(deg, ln, w):
        a = math.radians(deg)
        ca, sa = math.cos(a), math.sin(a)
        draw.polygon([(c + ca * ln, c + sa * ln), (c - sa * w, c + ca * w),
                      (c - ca * ln, c - sa * ln), (c + sa * w, c - ca * w)], fill=(255, 255, 240, 255))

    ray(angle, length, length * 0.09)
    ray(angle + 90, length, length * 0.09)
    ray(angle + 45, length * 0.45, length * 0.06)
    ray(angle + 135, length * 0.45, length * 0.06)
    out = Image.alpha_composite(im.filter(ImageFilter.GaussianBlur(length * 0.18)), im)
    r = length * 0.12
    ImageDraw.Draw(out).ellipse((c - r, c - r, c + r, c + r), fill=(255, 255, 255, 255))
    return out.resize((max(1, side // 4),) * 2, Image.LANCZOS)


def main():
    logo = Image.open(HERE / "logo.png")
    h = round(logo.height * LOGO_W / logo.width)
    with tempfile.TemporaryDirectory() as tmp:
        for f in range(FRAMES):
            t = f / FPS
            frame = Image.new("RGBA", (LOGO_W + 2 * PAD, h + 2 * PAD), (0, 0, 0, 0))
            for x, y, t0, dur, size in SPARKS:
                p = (t - t0) / dur
                if 0 <= p <= 1:
                    s = star(max(1, int(40 * size * math.sin(math.pi * p))), p * 40)
                    frame.alpha_composite(s, (int(PAD + x * LOGO_W - s.width / 2),
                                              int(PAD + y * h - s.height / 2)))
            frame.save(Path(tmp) / f"f{f:03d}.png")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS),
                        "-i", str(Path(tmp) / "f%03d.png"), "-c:v", "png", "-pix_fmt", "rgba",
                        str(HERE / "logo_sparkles.mov")], check=True)


if __name__ == "__main__":
    sys.exit(main())
