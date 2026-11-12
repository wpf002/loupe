"""Synthetic items. No real photo, name or UUID appears here."""

import subprocess  # noqa: S404 - runs only the bundled ffmpeg
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from loupe.audio import ffmpeg_exe
from loupe.brand import BrandProfile, Colors, Fonts, Voice
from loupe.models import LibraryItem, MediaType

UUID_A = "00000000-0000-4000-8000-00000000000A"
UUID_B = "00000000-0000-4000-8000-00000000000B"
UUID_C = "00000000-0000-4000-8000-00000000000C"


def make_item(uuid: str = UUID_A, **overrides: object) -> LibraryItem:
    base = LibraryItem(
        uuid=uuid,
        taken_at=datetime(2024, 5, 10, 12, 0, tzinfo=UTC),
        media_type=MediaType.PHOTO,
        duration_seconds=None,
        apple_score=0.5,
        preview_path="/synthetic/preview.jpeg",
        albums=("CC Travel",),
        labels=("Beach",),
        persons=("Test Person",),
        is_hidden=False,
        is_screenshot=False,
        is_screen_recording=False,
        is_burst=False,
        original_is_local=False,
    )
    return replace(base, **overrides)


def synthetic_uuid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012X}"


def seed_vectors(
    connection: object, store: object, model_id: str, vectors: dict, first_row: int = 0
) -> None:
    """Write vectors for already-scanned UUIDs, as `loupe embed` would."""
    for row, (uuid, vector) in enumerate(vectors.items(), start=first_row):
        store.write(row, vector[None, :])  # type: ignore[attr-defined]
        connection.execute(  # type: ignore[attr-defined]
            "INSERT INTO embeddings (model, uuid, vector_row, embedded_at) VALUES (?, ?, ?, ?)",
            (model_id, uuid, row, "2024-01-01T00:00:00+00:00"),
        )
    connection.commit()  # type: ignore[attr-defined]


SYSTEM_FONT = Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")


def make_brand(tmp_path: Path) -> BrandProfile:
    """A synthetic brand. The font is a stock macOS font copied into tmp_path."""
    font = tmp_path / "fonts" / "Display.ttf"
    font.parent.mkdir(parents=True, exist_ok=True)
    font.write_bytes(SYSTEM_FONT.read_bytes())
    return BrandProfile(
        name="Test Studio",
        colors=Colors(ink="#111111", paper="#F3EFE6", accent="#A8681A", secondary="#445566"),
        fonts=Fonts(display=font, body=font),
        voice=Voice(
            traits=("warm", "direct"),
            avoid=("hustle",),
            comment_word="guide",
            samples=("First caption.", "Second caption.", "Third caption."),
        ),
    )


def synthetic_clip(target: Path, seconds: int, size: str = "160x120") -> Path:
    subprocess.run(  # noqa: S603 - bundled ffmpeg, fixed argv
        [ffmpeg_exe(), "-v", "error",
         "-f", "lavfi", "-i", f"testsrc=size={size}:rate=10:d={seconds}",
         "-f", "lavfi", "-i", f"sine=duration={seconds}",
         "-shortest", "-pix_fmt", "yuv420p", str(target)],
        check=True,
    )  # fmt: skip
    return target


def probe_seconds(path: Path) -> float:
    probe = subprocess.run(  # noqa: S603 - bundled ffmpeg, fixed argv
        [ffmpeg_exe(), "-i", str(path)], capture_output=True, text=True, check=False
    )
    hours, minutes, seconds = probe.stderr.split("Duration: ")[1].split(",")[0].split(":")
    return float(seconds) + int(minutes) * 60 + int(hours) * 3600
