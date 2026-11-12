"""Decide whether an item is off-limits. Pure functions only."""

from loupe.config import Exclusions
from loupe.models import ExclusionReason, LibraryItem


def exclusion_reason(item: LibraryItem, exclusions: Exclusions) -> ExclusionReason | None:
    """Return the first rule that excludes the item, or None if it is eligible."""
    if item.is_hidden:
        return ExclusionReason.HIDDEN
    if item.uuid.upper() in exclusions.uuids:
        return ExclusionReason.UUID
    if exclusions.albums.intersection(item.albums):
        return ExclusionReason.ALBUM
    if exclusions.persons.intersection(item.persons):
        return ExclusionReason.PERSON
    if item.taken_at is not None and any(
        date_range.contains(item.taken_at.date()) for date_range in exclusions.date_ranges
    ):
        return ExclusionReason.DATE
    return None
