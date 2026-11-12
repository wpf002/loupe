"""M9: cut dead air and filler words from a take, using transcript word timestamps.

Writes only to data/renders/. The original in Photos is read, never touched.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from loupe.audio import run_ffmpeg
from loupe.transcribe import Transcript, Word

FILLER_WORDS = frozenset({"um", "umm", "uh", "uhh", "uhm", "er", "erm", "hmm", "mm", "ah"})
MAX_GAP_SECONDS = 0.35
PAD_SECONDS = 0.08
RENDER_TIMEOUT_SECONDS = 1800
VIDEO_CODEC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
AUDIO_CODEC = ["-c:a", "aac", "-b:a", "160k"]
_LETTERS = re.compile(r"[^a-z']")


@dataclass(frozen=True)
class TimeRange:
    start: float
    end: float

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class CutPlan:
    ranges: tuple[TimeRange, ...]
    fillers_removed: int
    source_seconds: float

    @property
    def kept_seconds(self) -> float:
        return sum(time_range.length for time_range in self.ranges)


def is_filler(word: Word) -> bool:
    return _LETTERS.sub("", word.text.lower()) in FILLER_WORDS


def plan_cut(
    transcript: Transcript,
    source_seconds: float,
    max_gap: float = MAX_GAP_SECONDS,
    pad: float = PAD_SECONDS,
) -> CutPlan:
    """Keep spoken words, join ones closer than max_gap, pad each kept range."""
    spoken = [word for word in transcript.words if not is_filler(word)]
    ranges: list[TimeRange] = []
    for word in spoken:
        start = max(0.0, word.start - pad)
        end = min(source_seconds, word.end + pad)
        if ranges and start - ranges[-1].end <= max_gap:
            ranges[-1] = TimeRange(ranges[-1].start, max(ranges[-1].end, end))
        else:
            ranges.append(TimeRange(start, end))
    return CutPlan(
        ranges=tuple(ranges),
        fillers_removed=len(transcript.words) - len(spoken),
        source_seconds=source_seconds,
    )


def remap_words(transcript: Transcript, plan: CutPlan) -> tuple[Word, ...]:
    """Word timings on the cut timeline, for captions. Fillers and words outside ranges drop."""
    remapped: list[Word] = []
    offset = 0.0
    words = [word for word in transcript.words if not is_filler(word)]
    for time_range in plan.ranges:
        for word in words:
            if word.start >= time_range.start and word.end <= time_range.end:
                remapped.append(
                    Word(
                        word.text,
                        round(word.start - time_range.start + offset, 3),
                        round(word.end - time_range.start + offset, 3),
                    )
                )
        offset += time_range.length
    return tuple(remapped)


def select_expression(ranges: tuple[TimeRange, ...]) -> str:
    return "+".join(f"between(t,{r.start:.3f},{r.end:.3f})" for r in ranges)


def cut_arguments(source: Path, target: Path, plan: CutPlan) -> list[str]:
    keep = select_expression(plan.ranges)
    filters = (
        f"[0:v]select='{keep}',setpts=N/FRAME_RATE/TB[v];[0:a]aselect='{keep}',asetpts=N/SR/TB[a]"
    )
    return [
        "-y", "-i", str(source), "-filter_complex", filters,
        "-map", "[v]", "-map", "[a]", *VIDEO_CODEC, *AUDIO_CODEC,
        "-movflags", "+faststart", str(target),
    ]  # fmt: skip


def render_cut(source: Path, target: Path, plan: CutPlan) -> Path:
    if not plan.ranges:
        raise ValueError("nothing to keep: the transcript has no spoken words")
    target.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(cut_arguments(source, target, plan), timeout=RENDER_TIMEOUT_SECONDS)
    return target


def cut_path(renders_dir: Path, uuid: str) -> Path:
    return renders_dir / f"{uuid}-cut.mp4"
