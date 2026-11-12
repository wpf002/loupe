"""Local HTML contact sheets. Thumbnails are downsized copies written under data/."""

import html
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from PIL import Image, ImageOps

from loupe.embed import load_preview

THUMBNAIL_EDGE = 480
THUMBNAIL_QUALITY = 82


@dataclass(frozen=True)
class Tile:
    uuid: str
    preview_path: str | None
    media_type: str
    score: float | None = None


@dataclass(frozen=True)
class Section:
    title: str
    tiles: list[Tile]
    total: int


def theme_css() -> str:
    return files("loupe.web").joinpath("theme.css").read_text()


def thumbnail(tile: Tile, thumbs_dir: Path) -> Path | None:
    """Write a small JPEG once per UUID. Returns None when the preview can't be read."""
    target = thumbs_dir / f"{tile.uuid}.jpg"
    if target.is_file():
        return target
    image = load_preview(tile.preview_path)
    if not isinstance(image, Image.Image):
        return None
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    ImageOps.contain(image, (THUMBNAIL_EDGE, THUMBNAIL_EDGE)).save(
        target, "JPEG", quality=THUMBNAIL_QUALITY
    )
    return target


def _tile_html(tile: Tile, src: str) -> str:
    uuid = html.escape(tile.uuid)
    badge = '<span class="badge">Video</span>' if tile.media_type == "video" else ""
    score = f'<span class="score">{tile.score:.0%}</span>' if tile.score is not None else ""
    return (
        f'<figure class="tile">{badge}<img loading="lazy" src="{html.escape(src)}" alt="">'
        f'<figcaption>{score}<button class="copy" data-uuid="{uuid}" '
        f'title="Copy UUID">{uuid[:8]}</button></figcaption></figure>'
    )


def _section_html(section: Section, page_dir: Path, thumbs_dir: Path) -> str:
    tiles = []
    for tile in section.tiles:
        path = thumbnail(tile, thumbs_dir)
        if path is not None:
            tiles.append(_tile_html(tile, path.relative_to(page_dir).as_posix()))
    body = "".join(tiles) if tiles else '<p class="empty">Nothing here yet.</p>'
    shown = f"{len(tiles)} of {section.total}" if len(tiles) < section.total else str(section.total)
    return (
        f'<section class="section"><div class="section-head"><h2>{html.escape(section.title)}</h2>'
        f'<span class="count num">{shown}</span></div><div class="grid">{body}</div></section>'
    )


COPY_SCRIPT = """
document.addEventListener('click', (event) => {
  const button = event.target.closest('.copy');
  if (!button) return;
  navigator.clipboard.writeText(button.dataset.uuid);
  button.textContent = 'Copied';
});
"""


def page(title: str, body: str, stats: dict[str, int] | None = None, home: str = "") -> str:
    stat_html = "".join(
        f'<div class="stat"><b class="num">{value:,}</b><span>{html.escape(label)}</span></div>'
        for label, value in (stats or {}).items()
    )
    brand = (
        f'<a class="brand" href="{html.escape(home)}">Loupe</a>'
        if home
        else ('<span class="brand">Loupe</span>')
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)} · Loupe</title><style>{theme_css()}</style></head><body>"
        f'<header class="masthead"><div>{brand}<h1>{html.escape(title)}</h1></div>'
        f'<div class="stats">{stat_html}</div></header>{body}'
        f"<script>{COPY_SCRIPT}</script></body></html>"
    )


def write_sheet(
    path: Path,
    title: str,
    sections: list[Section],
    thumbs_dir: Path,
    stats: dict[str, int] | None = None,
    home: str = "",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(_section_html(section, path.parent, thumbs_dir) for section in sections)
    path.write_text(page(title, body, stats, home))
    return path


def slug(name: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in name)
    return "-".join(part for part in cleaned.split("-") if part) or "untitled"
