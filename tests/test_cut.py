import subprocess
from pathlib import Path

import pytest

from loupe.audio import ffmpeg_exe, run_ffmpeg
from loupe.cut import TimeRange, cut_path, is_filler, plan_cut, remap_words, render_cut
from loupe.transcribe import Transcript, Word

TAKE = Transcript(
    language="en",
    words=(
        Word(" So", 1.00, 1.20),
        Word(" um,", 1.30, 1.60),
        Word(" this", 1.70, 1.90),
        Word(" works", 1.95, 2.30),
        Word(" really", 5.00, 5.40),
        Word(" well.", 5.45, 5.90),
    ),
)


def test_is_filler_ignores_case_and_punctuation() -> None:
    assert is_filler(Word(" Um,", 0, 1))
    assert not is_filler(Word(" umbrella", 0, 1))


def test_plan_drops_filler_and_dead_air() -> None:
    plan = plan_cut(TAKE, source_seconds=8.0)
    assert plan.fillers_removed == 1
    assert plan.ranges == (TimeRange(0.92, 2.38), TimeRange(4.92, 5.98))
    assert plan.kept_seconds == pytest.approx(2.52)


def test_ranges_clamp_to_clip() -> None:
    take = Transcript(language="en", words=(Word(" hi", 0.0, 0.99),))
    assert plan_cut(take, source_seconds=1.0).ranges == (TimeRange(0.0, 1.0),)


def test_remap_words_on_cut_timeline() -> None:
    plan = plan_cut(TAKE, source_seconds=8.0)
    words = remap_words(TAKE, plan)
    assert [word.text for word in words] == [" So", " this", " works", " really", " well."]
    assert words[0].start == pytest.approx(0.08)
    assert words[3].start == pytest.approx(1.46 + 0.08)


def test_render_cut_shortens_synthetic_clip(tmp_path: Path) -> None:
    source = tmp_path / "take.mp4"
    subprocess.run(  # noqa: S603
        [ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:d=8",
         "-f", "lavfi", "-i", "sine=duration=8", "-shortest", "-pix_fmt", "yuv420p", str(source)],
        check=True,
    )  # fmt: skip
    target = render_cut(source, cut_path(tmp_path / "renders", "SYNTHETIC"), plan_cut(TAKE, 8.0))
    probe = subprocess.run(  # noqa: S603
        [ffmpeg_exe(), "-i", str(target)], capture_output=True, text=True, check=False
    )
    duration = probe.stderr.split("Duration: ")[1].split(",")[0]
    hours, minutes, seconds = duration.split(":")
    assert 2.0 < float(seconds) + int(minutes) * 60 + int(hours) * 3600 < 3.2
    assert run_ffmpeg(["-i", str(target), "-f", "null", "-"]) == b""


def test_render_refuses_empty_plan(tmp_path: Path) -> None:
    silent = plan_cut(Transcript(language=None, words=()), 4.0)
    with pytest.raises(ValueError, match="nothing to keep"):
        render_cut(tmp_path / "x.mp4", tmp_path / "y.mp4", silent)
