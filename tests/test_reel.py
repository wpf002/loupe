from pathlib import Path

import pytest

from loupe.audio import run_ffmpeg
from loupe.reel import (
    END_CARD_SECONDS,
    ReelSpec,
    ass_colour,
    ass_script,
    ass_text,
    ass_time,
    caption_events,
    font_family,
    reel_path,
    render_reel,
)
from loupe.transcribe import Word
from tests.fixtures import SYSTEM_FONT, make_brand, probe_seconds, synthetic_clip

WORDS = tuple(Word(f" w{number}", number * 0.5, number * 0.5 + 0.4) for number in range(8))
needs_font = pytest.mark.skipif(not SYSTEM_FONT.is_file(), reason="stock macOS font missing")


def test_ass_colour_is_bgr() -> None:
    assert ass_colour("#A8681A") == "&H001A68A8"


def test_ass_time_formats_centiseconds() -> None:
    assert ass_time(65.432) == "0:01:05.43"
    assert ass_time(-1) == "0:00:00.00"


def test_ass_text_strips_override_tags() -> None:
    assert ass_text(" {\\b1}hi\\N ") == "(b1)hiN"


def test_caption_events_highlight_each_word_and_stop_at_body() -> None:
    events = caption_events(WORDS, body_seconds=2.0, accent="&H00FF0000")
    assert len(events) == 4
    assert "{\\c&H00FF0000}w0{\\r} w1 w2" in events[0]
    assert events[3].split(",")[2] == "0:00:01.90"


@needs_font
def test_script_has_hook_and_end_card(tmp_path: Path) -> None:
    brand = make_brand(tmp_path)
    script = ass_script(brand, ReelSpec("Pack light", 4.0, "GUIDE"), WORDS)
    assert font_family(brand.fonts.display) in script
    assert "Hook,,0,0,0,,Pack light" in script
    assert "Comment\\N{\\c&H001A68A8}GUIDE" in script


@needs_font
def test_render_reel_is_vertical_with_end_card(tmp_path: Path) -> None:
    clip = synthetic_clip(tmp_path / "cut.mp4", seconds=6)
    spec = ReelSpec(hook="Pack light", body_seconds=4.0, comment_word="GUIDE")
    target = render_reel(
        clip, reel_path(tmp_path / "renders", "SYNTHETIC", 1), make_brand(tmp_path), spec, WORDS
    )
    assert probe_seconds(target) == pytest.approx(4.0 + END_CARD_SECONDS, abs=0.3)
    frame = run_ffmpeg(
        ["-ss", "5.5", "-i", str(target), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", "4x4", "-"]
    )  # fmt: skip
    assert len(frame) == 4 * 4 * 3
    red, green, blue = frame[0], frame[1], frame[2]
    assert (red, green, blue) == pytest.approx((0xF3, 0xEF, 0xE6), abs=12)
