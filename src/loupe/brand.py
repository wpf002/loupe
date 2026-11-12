"""M7: one brand profile in config/brand.toml. Read by hooks, week, carousel and the app."""

import html
import re
import tomllib
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

DEFAULT_BRAND_PATH = Path("config/brand.toml")
FONT_SUFFIXES = (".ttf", ".otf", ".ttc")
HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
MIN_VOICE_SAMPLES = 3
MAX_COMMENT_WORD = 20


class BrandError(Exception):
    """Raised when config/brand.toml is missing or invalid."""


def _hex(value: str) -> str:
    if not HEX_COLOR.match(value):
        raise ValueError(f"not a #RRGGBB colour: {value!r}")
    return value.upper()


def _font(value: Path) -> Path:
    path = value.expanduser()
    if path.suffix.lower() not in FONT_SUFFIXES:
        raise ValueError(f"{path.name} is not a .ttf, .otf or .ttc file")
    if not path.is_file():
        raise ValueError(f"font file not found: {path}")
    return path


def _comment_word(value: str) -> str:
    word = value.strip()
    if not word or len(word) > MAX_COMMENT_WORD or not word.replace("-", "").isalnum():
        raise ValueError("comment_word must be one short word")
    return word.upper()


HexColor = Annotated[str, AfterValidator(_hex)]
FontFile = Annotated[Path, AfterValidator(_font)]
CommentWord = Annotated[str, AfterValidator(_comment_word)]


class Colors(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ink: HexColor
    paper: HexColor
    accent: HexColor
    secondary: HexColor


class Fonts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    display: FontFile
    body: FontFile


class Voice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    traits: tuple[str, ...] = ()
    avoid: tuple[str, ...] = ()
    comment_word: CommentWord = "LINK"
    samples: tuple[str, ...] = Field(min_length=MIN_VOICE_SAMPLES)


class BrandProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1)
    colors: Colors
    fonts: Fonts
    voice: Voice


def load_brand(path: Path = DEFAULT_BRAND_PATH) -> BrandProfile:
    if not path.is_file():
        raise BrandError(f"missing {path}. Copy config/brand.example.toml and fill it in.")
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        return BrandProfile.model_validate(raw)
    except tomllib.TOMLDecodeError as error:
        raise BrandError(f"{path} is not valid TOML: {error}") from error
    except ValidationError as error:
        raise BrandError(f"invalid brand profile: {error}") from error


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


def brand_preview_html(brand: BrandProfile) -> str:
    """Swatches, font specimens and one caption card, for checking the profile by eye."""
    from loupe.sheets import page

    colors = brand.colors
    swatches = "".join(
        f'<div class="swatch"><i style="background:{value}"></i>'
        f'<b>{html.escape(name.title())}</b><span class="num">{value}</span></div>'
        for name, value in colors.model_dump().items()
    )
    sample = html.escape(brand.voice.samples[0])
    style = f"""
    <style>
    @font-face {{ font-family: BrandDisplay; src: url("{brand.fonts.display.as_uri()}"); }}
    @font-face {{ font-family: BrandBody; src: url("{brand.fonts.body.as_uri()}"); }}
    .brand-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-6);
      padding: var(--space-6) var(--space-7); }}
    .swatches {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: var(--space-4); }}
    .swatch i {{ display: block; height: 96px; border-radius: var(--radius-m);
      border: 1px solid var(--line); margin-bottom: var(--space-2); }}
    .swatch span {{ display: block; color: var(--muted); font-size: 13px; }}
    .card {{ aspect-ratio: 4 / 5; border-radius: var(--radius-l); padding: 48px;
      display: flex; flex-direction: column; justify-content: space-between;
      background: {colors.paper}; color: {colors.ink}; box-shadow: var(--shadow); }}
    .card h2 {{ font: 64px/1 BrandDisplay; margin: 0; }}
    .card p {{ font: 20px/1.45 BrandBody; margin: 0; }}
    .card em {{ font: 14px BrandBody; font-style: normal; letter-spacing: .12em;
      color: {colors.accent}; }}
    </style>"""
    body = (
        f'{style}<div class="brand-grid"><div class="swatches">{swatches}</div>'
        f'<div class="card"><em>{html.escape(brand.name.upper())}</em>'
        f"<h2>Hook goes here</h2><p>{sample}</p></div></div>"
    )
    return page("Brand", body, {"Voice Samples": len(brand.voice.samples)})
