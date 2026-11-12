"""Records passed between the library reader, the exclusion rules and the index."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class MediaType(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"


class ExclusionReason(StrEnum):
    HIDDEN = "hidden"
    UUID = "uuid"
    ALBUM = "album"
    PERSON = "person"
    DATE = "date"


@dataclass(frozen=True)
class LibraryItem:
    """One photo or video as read from Photos. Persons holds named people only."""

    uuid: str
    taken_at: datetime | None
    media_type: MediaType
    duration_seconds: float | None
    apple_score: float | None
    preview_path: str | None
    albums: tuple[str, ...]
    labels: tuple[str, ...]
    persons: tuple[str, ...]
    is_hidden: bool
    is_screenshot: bool
    is_screen_recording: bool
    is_burst: bool
    original_is_local: bool
