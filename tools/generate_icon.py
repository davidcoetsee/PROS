"""Generate the PROS application icon from simple vector-like shapes."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "PROS.ico"
PNG_OUT = ROOT / "assets" / "PROS.png"


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        Path(r"C:\Windows\Fonts\segoeuib.ttf"),
        Path(r"C:\Windows\Fonts\arialbd.ttf"),
    )
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def build_icon() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    draw.rounded_rectangle((8, 8, 248, 248), radius=54, fill="#12243A")
    draw.rounded_rectangle((50, 30, 206, 226), radius=18, fill="#F8FAFC")
    draw.polygon(((164, 30), (206, 72), (164, 72)), fill="#DDE6EF")

    colors = ("#0E7C86", "#2878B5", "#D4862F", "#C94E5C")
    letters = "PROS"
    font = _font(41)
    for index, (letter, color) in enumerate(zip(letters, colors, strict=True)):
        y = 52 + index * 39
        draw.rounded_rectangle((69, y + 8, 87, y + 26), radius=5, fill=color)
        draw.text((99, y), letter, font=font, fill="#12243A")

    canvas.save(
        OUT,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    canvas.save(PNG_OUT, format="PNG", optimize=True)


if __name__ == "__main__":
    build_icon()
