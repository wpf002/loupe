"""M10: render a cut take as a 9:16 reel with the picked hook, word-by-word captions in
the brand font and colours, and an end card asking for the comment word.

Writes to data/renders/ only. ffmpeg comes from imageio-ffmpeg; captions go through libass.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from PIL import ImageFont

from loupe.audio import run_ffmpeg
from loupe.brand import BrandProfile, hex_to_rgb
from loupe.cut import AUDIO_CODEC, RENDER_TIMEOUT_SECONDS, VIDEO_CODEC
from loupe.transcribe import Word

WIDTH = 1080
HEIGHT = 1920
HOOK_SECONDS = 3.0
END_CARD_SECONDS = 2.5
WORDS_PER_LINE = 3
DEFAULT_LENGTH_SECONDS = 30.0
CAPTION_SIZE = 92
HOOK_SIZE = 72
END_SIZE = 120
_UNSAFE_FILTER_CHARACTERS = ("'", "\\", "\n")


@dataclass(frozen=True)
class ReelSpec:
    hook: str
    body_seconds: float
    comment_word: str

    @property
    def total_seconds(self) -> float:
        return self.body_seconds + END_CARD_SECONDS


def ass_colour(hex_colour: str, alpha: int = 0) -> str:
    """ASS colours are &HAABBGGRR."""
    red, green, blue = hex_to_rgb(hex_colour)
    return f"&H{alpha:02X}{blue:02X}{green:02X}{red:02X}"


def ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, rest = divmod(centiseconds, 360_000)
    minutes, rest = divmod(rest, 6_000)
    whole, fraction = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{whole:02d}.{fraction:02d}"


def ass_text(text: str) -> str:
    return text.strip().replace("\\", "").replace("{", "(").replace("}", ")")


def font_family(font_path: Path) -> str:
    return ImageFont.truetype(str(font_path), size=12).getname()[0] or font_path.stem


def caption_lines(words: tuple[Word, ...], body_seconds: float) -> Iterator[tuple[Word, ...]]:
    visible = [word for word in words if word.start < body_seconds and ass_text(word.text)]
    for index in range(0, len(visible), WORDS_PER_LINE):
        yield tuple(visible[index : index + WORDS_PER_LINE])


def caption_events(words: tuple[Word, ...], body_seconds: float, accent: str) -> list[str]:
    """One event per word: its line shown with the spoken word in the accent colour."""
    events: list[str] = []
    for line in caption_lines(words, body_seconds):
        for position, word in enumerate(line):
            is_last = position + 1 == len(line)
            end = word.end if is_last else line[position + 1].start
            parts = [
                f"{{\\c{accent}}}{ass_text(other.text)}{{\\r}}" if other is word
                else ass_text(other.text)
                for other in line
            ]  # fmt: skip
            start, stop = word.start, min(max(end, word.start + 0.05), body_seconds)
            events.append(
                f"Dialogue: 1,{ass_time(start)},{ass_time(stop)},Caption,,0,0,0,,{' '.join(parts)}"
            )
    return events


def ass_script(brand: BrandProfile, spec: ReelSpec, words: tuple[Word, ...]) -> str:
    family = font_family(brand.fonts.display)
    ink, paper = ass_colour(brand.colors.ink), ass_colour(brand.colors.paper)
    accent = ass_colour(brand.colors.accent)
    style_format = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
    )
    styles = [
        f"Style: Caption,{family},{CAPTION_SIZE},{paper},{paper},{ink},{ink},0,0,0,0,"
        "100,100,0,0,1,7,0,2,90,90,520,1",
        f"Style: Hook,{family},{HOOK_SIZE},{ink},{ink},{paper},{paper},0,0,0,0,"
        "100,100,0,0,3,28,0,8,110,110,300,1",
        f"Style: Card,{family},{END_SIZE},{ink},{ink},{paper},{paper},0,0,0,0,"
        "100,100,0,0,1,0,0,5,90,90,0,1",
    ]
    end_start, end_stop = ass_time(spec.body_seconds), ass_time(spec.total_seconds)
    card = f"{{\\p1\\c{paper}}}m 0 0 l {WIDTH} 0 {WIDTH} {HEIGHT} 0 {HEIGHT}{{\\p0}}"
    events = [
        f"Dialogue: 2,{ass_time(0)},{ass_time(min(HOOK_SECONDS, spec.body_seconds))},"
        f"Hook,,0,0,0,,{ass_text(spec.hook)}",
        *caption_events(words, spec.body_seconds, accent),
        f"Dialogue: 3,{end_start},{end_stop},Card,,0,0,0,,{{\\an7\\pos(0,0)}}{card}",
        f"Dialogue: 4,{end_start},{end_stop},Card,,0,0,0,,Comment\\N"
        f"{{\\c{accent}}}{ass_text(spec.comment_word)}",
    ]
    return "\n".join(
        [
            "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {WIDTH}", f"PlayResY: {HEIGHT}",
            "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
            "[V4+ Styles]", style_format, *styles, "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            *events, "",
        ]
    )  # fmt: skip


def _filter_path(path: Path) -> str:
    text = str(path.resolve())
    if any(character in text for character in _UNSAFE_FILTER_CHARACTERS):
        raise ValueError("render path contains a quote or backslash")
    return text


def reel_arguments(
    cut_video: Path, subtitles: Path, fonts_dir: Path, spec: ReelSpec, target: Path
) -> list[str]:
    body, total = f"{spec.body_seconds:.3f}", f"{spec.total_seconds:.3f}"
    video = (
        f"[0:v]trim=0:{body},setpts=PTS-STARTPTS,"
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},"
        f"setsar=1,tpad=stop_mode=clone:stop_duration={END_CARD_SECONDS},"
        f"ass=filename='{_filter_path(subtitles)}':fontsdir='{_filter_path(fonts_dir)}'[v]"
    )
    audio = f"[0:a]atrim=0:{body},asetpts=PTS-STARTPTS,apad=whole_dur={total}[a]"
    return [
        "-y", "-i", str(cut_video), "-filter_complex", f"{video};{audio}",
        "-map", "[v]", "-map", "[a]", *VIDEO_CODEC, *AUDIO_CODEC, "-t", total,
        "-movflags", "+faststart", str(target),
    ]  # fmt: skip


def reel_path(renders_dir: Path, uuid: str, hook_number: int) -> Path:
    return renders_dir / f"{uuid}-reel-{hook_number}.mp4"


def render_reel(
    cut_video: Path,
    target: Path,
    brand: BrandProfile,
    spec: ReelSpec,
    words: tuple[Word, ...],
) -> Path:
    if spec.body_seconds <= 0:
        raise ValueError("reel length must be positive")
    target.parent.mkdir(parents=True, exist_ok=True)
    subtitles = target.with_suffix(".ass")
    subtitles.write_text(ass_script(brand, spec, words), encoding="utf-8")
    arguments = reel_arguments(cut_video, subtitles, brand.fonts.display.parent, spec, target)
    run_ffmpeg(arguments, timeout=RENDER_TIMEOUT_SECONDS)
    return target
