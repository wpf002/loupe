"""M13: render the week's story frames as 9:16 PNGs, same look as the carousels.

Writes to data/renders/ only; nothing is sent.
"""

from pathlib import Path

from loupe.brand import BrandProfile
from loupe.carousel import Canvas, render_sequence

# Instagram draws the progress bar and profile over the top band and the reply box
# over the bottom band, so text stays out of both.
STORY_CANVAS = Canvas(width=1080, height=1920, top=260, bottom=340)


def render_story(
    brand: BrandProfile, frames: list[str], cover_preview: Path | None, target_dir: Path
) -> list[Path]:
    return render_sequence(brand, frames, cover_preview, target_dir, STORY_CANVAS, prefix="frame")


def story_dir(renders_dir: Path, week_start: str, day: str) -> Path:
    return renders_dir / "story" / f"{week_start}-{day.lower()}"
