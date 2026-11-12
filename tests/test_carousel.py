from pathlib import Path

import pytest
from PIL import Image, ImageFont

from loupe.brand import hex_to_rgb
from loupe.carousel import (
    HEIGHT,
    WIDTH,
    carousel_dir,
    cover_background,
    fit_text,
    render_carousel,
    wrap,
)
from tests.fixtures import SYSTEM_FONT, make_brand

pytestmark = pytest.mark.skipif(not SYSTEM_FONT.is_file(), reason="stock macOS font missing")
SLIDES = ["Three things I never pack", "A second blazer.", "Full-size toiletries.", "Comment GUIDE"]


def test_wrap_keeps_words_whole() -> None:
    font = ImageFont.truetype(str(SYSTEM_FONT), size=40)
    lines = wrap("pack light and travel further every single weekend", font, 300)
    assert len(lines) > 1
    assert all(font.getlength(line) <= 300 for line in lines)
    assert " ".join(lines) == "pack light and travel further every single weekend"


def test_fit_text_shrinks_long_copy() -> None:
    short = fit_text("Pack light", SYSTEM_FONT, 100, (800, 400))
    long = fit_text("Pack light " * 40, SYSTEM_FONT, 100, (800, 400))
    assert long.font.size < short.font.size


def test_unreadable_preview_falls_back_to_solid(tmp_path: Path) -> None:
    broken = tmp_path / "preview.heic"
    broken.write_bytes(b"not an image")
    image = cover_background(broken, (10, 20, 30))
    assert image.getpixel((5, 5)) == (10, 20, 30)


def test_render_carousel_writes_numbered_slides(tmp_path: Path) -> None:
    brand = make_brand(tmp_path)
    preview = tmp_path / "preview.png"
    Image.new("RGB", (400, 300), (200, 120, 40)).save(preview)
    paths = render_carousel(brand, SLIDES, preview, carousel_dir(tmp_path, "2026-11-16", "Tue"))
    assert [path.name for path in paths] == [f"slide-0{n}.png" for n in range(1, 5)]
    assert paths[0].parent.name == "2026-11-16-tue"
    with Image.open(paths[1]) as body:
        assert body.size == (WIDTH, HEIGHT)
        assert body.getpixel((WIDTH // 2, HEIGHT - 10)) == hex_to_rgb(brand.colors.paper)
    with Image.open(paths[0]) as cover:
        assert cover.getpixel((10, 10)) == pytest.approx((200, 120, 40), abs=3)


def test_render_carousel_needs_slides(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one"):
        render_carousel(make_brand(tmp_path), [], None, tmp_path)
