"""Synthetic items. No real photo, name or UUID appears here."""

from dataclasses import replace
from datetime import UTC, datetime

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
