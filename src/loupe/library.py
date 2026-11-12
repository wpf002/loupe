"""Read-only adapter over osxphotos (checked against osxphotos 0.78.1).

Nothing here writes to the Photos library.
"""

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from osxphotos import PhotosDB
from osxphotos.utils import get_system_library_path

from loupe.models import LibraryItem, MediaType

# osxphotos names unnamed faces with this value (osxphotos._constants._UNKNOWN_PERSON).
UNNAMED_PERSON = "_UNKNOWN_"

UUID_LENGTH = 36
PREVIEW_EXTENSIONS = frozenset({".jpeg", ".jpg", ".heic", ".png"})
DERIVATIVE_ROOTS = ("resources/derivatives",)
SCOPED_DERIVATIVE_GLOB = "scopes/*/resources/derivatives"


class LibraryNotFoundError(Exception):
    """Raised when no Photos library can be located."""


@dataclass(frozen=True)
class LibraryStats:
    total: int
    with_preview: int
    originals_local: int


def resolve_library_path(configured: Path | None) -> Path:
    if configured is not None:
        if not configured.is_dir():
            raise LibraryNotFoundError(f"LOUPE_PHOTOS_LIBRARY does not exist: {configured}")
        return configured
    system_library = get_system_library_path()
    if system_library is None:
        raise LibraryNotFoundError("no system Photos library is set for this macOS account")
    path = Path(system_library)
    if not path.is_dir():
        raise LibraryNotFoundError(f"system Photos library is missing on disk: {path}")
    return path


def open_library(library_path: Path) -> PhotosDB:
    return PhotosDB(dbfile=str(library_path))


def _derivative_dirs(library_path: Path) -> list[Path]:
    roots = [library_path / root for root in DERIVATIVE_ROOTS]
    roots.extend(library_path.glob(SCOPED_DERIVATIVE_GLOB))
    return [root for root in roots if root.is_dir()]


def build_preview_index(library_path: Path) -> dict[str, str]:
    """Map UUID to its largest local preview image.

    One directory walk instead of osxphotos' per-photo listing, which is too slow
    at 200k items. Video derivatives that are images are the poster frames.
    """
    best: dict[str, tuple[int, str]] = {}
    for root in _derivative_dirs(library_path):
        for directory, _subdirs, filenames in os.walk(root):
            for filename in filenames:
                if Path(filename).suffix.lower() not in PREVIEW_EXTENSIONS:
                    continue
                uuid = filename[:UUID_LENGTH].upper()
                path = os.path.join(directory, filename)
                size = os.path.getsize(path)
                if uuid not in best or size > best[uuid][0]:
                    best[uuid] = (size, path)
    return {uuid: path for uuid, (_size, path) in best.items()}


def _preview_for(photo: Any, previews: dict[str, str]) -> str | None:
    found = previews.get(photo.uuid.upper())
    if found is not None:
        return found
    if photo.shared:
        shared = photo.path_derivatives
        return shared[0] if shared else None
    return None


def _duration(photo: Any) -> float | None:
    if not photo.ismovie or photo.exif_info is None:
        return None
    return photo.exif_info.duration


def _apple_score(photo: Any) -> float | None:
    score = photo.score
    return None if score is None else score.overall


def named_persons(names: list[str | None]) -> tuple[str, ...]:
    """Drop faces Photos has not named. osxphotos reports them as _UNKNOWN_ or None."""
    return tuple(name for name in names if name and name != UNNAMED_PERSON)


def to_library_item(photo: Any, previews: dict[str, str]) -> LibraryItem:
    return LibraryItem(
        uuid=photo.uuid,
        taken_at=photo.date,
        media_type=MediaType.VIDEO if photo.ismovie else MediaType.PHOTO,
        duration_seconds=_duration(photo),
        apple_score=_apple_score(photo),
        preview_path=_preview_for(photo, previews),
        albums=tuple(photo.albums),
        labels=tuple(photo.labels),
        persons=named_persons(photo.persons),
        is_hidden=bool(photo.hidden),
        is_screenshot=bool(photo.screenshot),
        is_screen_recording=bool(photo.screen_recording),
        is_burst=bool(photo.burst),
        original_is_local=not photo.ismissing and photo.path is not None,
    )


def sorted_photos(db: PhotosDB, limit: int | None) -> list[Any]:
    """Every non-deleted item, in UUID order so --limit picks the same slice each run."""
    photos = sorted(db.photos(intrash=False), key=lambda photo: photo.uuid)
    return photos if limit is None else photos[:limit]


def iter_library_items(library_path: Path, limit: int | None) -> Iterator[LibraryItem]:
    db = open_library(library_path)
    previews = build_preview_index(library_path)
    for photo in sorted_photos(db, limit):
        yield to_library_item(photo, previews)


def library_stats(library_path: Path) -> LibraryStats:
    db = open_library(library_path)
    previews = build_preview_index(library_path)
    photos = db.photos(intrash=False)
    return LibraryStats(
        total=len(photos),
        with_preview=sum(1 for photo in photos if _preview_for(photo, previews) is not None),
        originals_local=sum(1 for photo in photos if not photo.ismissing and photo.path),
    )


def original_paths(library_path: Path, uuids: list[str]) -> dict[str, Path]:
    """Paths of originals that are on disk. Read-only; never triggers an iCloud download."""
    if not uuids:
        return {}
    db = open_library(library_path)
    found: dict[str, Path] = {}
    for photo in db.photos(uuid=uuids, intrash=False):
        if not photo.ismissing and photo.path is not None:
            found[photo.uuid] = Path(photo.path)
    return found
