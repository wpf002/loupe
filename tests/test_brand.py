from pathlib import Path

import pytest

from loupe.brand import BrandError, brand_preview_html, hex_to_rgb, load_brand


def _write_profile(tmp_path: Path, **overrides: str) -> Path:
    display = tmp_path / "Display.ttf"
    body = tmp_path / "Body.otf"
    display.write_bytes(b"\x00")
    body.write_bytes(b"\x00")
    values = {
        "accent": "#a8681a",
        "display": str(display),
        "samples": '["one", "two", "three <b>"]',
        "comment_word": "guide",
    }
    values.update(overrides)
    path = tmp_path / "brand.toml"
    path.write_text(
        f"""
name = "Test Studio"
[colors]
ink = "#111111"
paper = "#F3EFE6"
accent = "{values["accent"]}"
secondary = "#445566"
[fonts]
display = "{values["display"]}"
body = "{body}"
[voice]
traits = ["warm"]
comment_word = "{values["comment_word"]}"
samples = {values["samples"]}
"""
    )
    return path


def test_valid_profile_normalises_values(tmp_path: Path) -> None:
    brand = load_brand(_write_profile(tmp_path))
    assert brand.colors.accent == "#A8681A"
    assert brand.voice.comment_word == "GUIDE"
    assert hex_to_rgb(brand.colors.accent) == (168, 104, 26)


@pytest.mark.parametrize(
    "overrides",
    [
        {"accent": "orange"},
        {"display": "/nowhere/Missing.ttf"},
        {"samples": '["only one"]'},
        {"comment_word": "two words"},
    ],
)
def test_invalid_profile_raises(tmp_path: Path, overrides: dict[str, str]) -> None:
    with pytest.raises(BrandError):
        load_brand(_write_profile(tmp_path, **overrides))


def test_non_font_file_rejected(tmp_path: Path) -> None:
    text_file = tmp_path / "notes.txt"
    text_file.write_text("x")
    with pytest.raises(BrandError):
        load_brand(_write_profile(tmp_path, display=str(text_file)))


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(BrandError, match="brand.example.toml"):
        load_brand(tmp_path / "absent.toml")


def test_preview_escapes_voice_text(tmp_path: Path) -> None:
    profile = _write_profile(tmp_path, samples='["<b>x", "y", "z"]')
    page_html = brand_preview_html(load_brand(profile))
    assert "&lt;b&gt;x" in page_html
    assert "#A8681A" in page_html
