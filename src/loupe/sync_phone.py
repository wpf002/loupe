"""Phone Sync: Loupe for iPhone sends item metadata as JSON, and the server builds the index.

The phone can't run SigLIP, so it sends no vectors. loupe-web embeds the previews itself.
The phone leaves hidden items out of its PhotoKit fetch, so they never reach the server.
"""

import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from loupe.config import AlbumName, Exclusions, PhotosUuid
from loupe.index import connect
from loupe.models import LibraryItem, MediaType
from loupe.scan import scan_items
from loupe.sync_index import export_index, import_index

MAX_PHONE_ITEMS = 400_000
MAX_ALBUMS_PER_ITEM = 200
MAX_DURATION_SECONDS = 24 * 3600


class PhoneSyncError(Exception):
    """Raised when the phone's upload can't become an index."""


class PhoneItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    uuid: PhotosUuid
    taken_at: datetime | None = None
    media_type: MediaType
    duration_seconds: Annotated[float, Field(ge=0, le=MAX_DURATION_SECONDS)] | None = None
    is_screenshot: bool = False
    is_screen_recording: bool = False
    is_burst: bool = False
    albums: Annotated[tuple[AlbumName, ...], Field(max_length=MAX_ALBUMS_PER_ITEM)] = ()


class PhoneUpload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: Annotated[tuple[PhoneItem, ...], Field(max_length=MAX_PHONE_ITEMS)]


def parse_upload(body: bytes) -> PhoneUpload:
    try:
        upload = PhoneUpload.model_validate_json(body)
    except ValidationError as error:
        raise PhoneSyncError("Library upload is invalid. Update Loupe, then sync again.") from error
    if len({item.uuid for item in upload.items}) != len(upload.items):
        raise PhoneSyncError("Library upload repeats an item")
    return upload


def to_library_item(item: PhoneItem) -> LibraryItem:
    return LibraryItem(
        uuid=item.uuid,
        taken_at=item.taken_at,
        media_type=item.media_type,
        duration_seconds=item.duration_seconds,
        apple_score=None,
        preview_path=None,
        albums=tuple(dict.fromkeys(item.albums)),
        labels=(),
        persons=(),
        is_hidden=False,
        is_screenshot=item.is_screenshot,
        is_screen_recording=item.is_screen_recording,
        is_burst=item.is_burst,
        original_is_local=False,
    )


def import_phone_items(
    index_path: Path, body: bytes, previews_dir: Path, exclusions: Exclusions
) -> int:
    """Replace the account's library with the phone's list. Returns the eligible count.

    The list goes through the same scan, export and import as a Mac sync, so excluded
    items are dropped and what the server generated (hooks, weeks, emails) is kept.
    """
    upload = parse_upload(body)
    with tempfile.TemporaryDirectory(prefix="loupe-phone-") as scratch:
        scanned = Path(scratch) / "scanned.sqlite"
        exported = Path(scratch) / "exported.sqlite"
        connection = connect(scanned)
        try:
            scan_items(connection, (to_library_item(item) for item in upload.items), exclusions)
        except sqlite3.Error as error:
            raise PhoneSyncError("Library upload could not be read") from error
        finally:
            connection.close()
        export_index(scanned, exported)
        return import_index(index_path, exported, previews_dir)
