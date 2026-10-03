"""Vygeneruje tři varianty ikony integrace do tools/ikony_varianty/{A,B,C}/.

    .venv/bin/python tools/make_icons.py        # potřebuje pillow
    ./tools/pouzit_ikonu.sh C                   # zkopíruje vybranou sadu do brand/

A: logo na průhledném pozadí (původní), B: stejné, ale větší, C: bílé logo na modrém
zaobleném čtverci. Sady A a B mají i tmavou variantu (světlejší modrá), C ne, protože
modrý čtverec je čitelný na světlém i tmavém tématu.
"""

import pathlib

import numpy as np
from PIL import Image, ImageDraw

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "tools" / "ikony_varianty"
BLUE = (0, 82, 156)
SIZES = {"icon.png": 256, "icon@2x.png": 512}


def load(name: str) -> Image.Image:
    return Image.open(ROOT / "docs" / name).convert("RGBA")


def on_transparent(logo: Image.Image, size: int, fill: float) -> Image.Image:
    width = int(size * fill)
    height = round(logo.height * width / logo.width)
    resized = logo.resize((width, height), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(resized, ((size - width) // 2, (size - height) // 2), resized)
    return canvas


def white(logo: Image.Image) -> Image.Image:
    data = np.asarray(logo).copy()
    data[..., 0] = data[..., 1] = data[..., 2] = 255
    return Image.fromarray(data, "RGBA")


def on_blue_square(logo: Image.Image, size: int, fill: float) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=int(size * 0.22), fill=BLUE + (255,)
    )
    inner = on_transparent(white(logo), size, fill)
    canvas.alpha_composite(inner)
    return canvas


def main() -> None:
    light, dark = load("logo.png"), load("logo-dark.png")
    sets = {
        "A": {"": lambda s: on_transparent(light, s, 0.92), "dark_": lambda s: on_transparent(dark, s, 0.92)},
        "B": {"": lambda s: on_transparent(light, s, 1.0), "dark_": lambda s: on_transparent(dark, s, 1.0)},
        "C": {"": lambda s: on_blue_square(light, s, 0.82)},
    }
    for variant, makers in sets.items():
        folder = OUT / variant
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("*.png"):
            old.unlink()
        for prefix, maker in makers.items():
            for name, size in SIZES.items():
                maker(size).save(folder / f"{prefix}{name}")
        print(variant, sorted(p.name for p in folder.glob("*.png")))


if __name__ == "__main__":
    main()
