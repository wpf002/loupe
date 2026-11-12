"""Load a preview from a local file (CLI) or from PhotoKit on demand (Loupe.app).

The app stores 'photokit:<uuid>' instead of a file path. Those bytes are pulled
from Photos when asked for and are not cached in bulk. Only covers for a planned
week are written to disk.
"""

from collections.abc import Callable
from enum import StrEnum
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

from loupe.photokit import EMBED_EDGE, uuid_from_preview_ref

register_heif_opener()

COVER_EDGE = 1440
COVER_QUALITY = 90

PreviewFetcher = Callable[[str, int], bytes | None]


class SkipReason(StrEnum):
    NO_PREVIEW = "no local preview"
    MISSING_FILE = "preview file missing"
    UNREADABLE = "unreadable image"
    NO_ACCESS = "Photos access not granted"


_photokit_fetch: PreviewFetcher | None = None


def enable_photokit(fetch: PreviewFetcher) -> None:
    """Called by the app once access is checked. Until then PhotoKit refs are skipped."""
    global _photokit_fetch  # noqa: PLW0603 - one switch for the process, set at app start
    _photokit_fetch = fetch


def disable_photokit() -> None:
    global _photokit_fetch  # noqa: PLW0603 - see enable_photokit
    _photokit_fetch = None


def _decode(source: Path | BytesIO) -> Image.Image | SkipReason:
    try:
        with Image.open(source) as image:
            return ImageOps.exif_transpose(image).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError):
        return SkipReason.UNREADABLE


def _photokit_bytes(uuid: str, edge: int) -> bytes | SkipReason:
    if _photokit_fetch is None:
        return SkipReason.NO_ACCESS
    data = _photokit_fetch(uuid, edge)
    return SkipReason.MISSING_FILE if data is None else data


def load_preview(preview_path: str | None, edge: int = EMBED_EDGE) -> Image.Image | SkipReason:
    if preview_path is None:
        return SkipReason.NO_PREVIEW
    uuid = uuid_from_preview_ref(preview_path)
    if uuid is not None:
        data = _photokit_bytes(uuid, edge)
        return data if isinstance(data, SkipReason) else _decode(BytesIO(data))
    path = Path(preview_path)
    if not path.is_file():
        return SkipReason.MISSING_FILE
    return _decode(path)


def preview_file(preview_path: str | None, cache_dir: Path) -> Path | None:
    """A file path for renderers that open the image themselves."""
    if preview_path is None:
        return None
    uuid = uuid_from_preview_ref(preview_path)
    if uuid is None:
        return Path(preview_path)
    target = cache_dir / f"{uuid}.jpg"
    if target.is_file():
        return target
    image = load_preview(preview_path, COVER_EDGE)
    if isinstance(image, SkipReason):
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    image.save(target, "JPEG", quality=COVER_QUALITY)
    return target
