"""M1: write library metadata into the index, applying exclusions."""

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from loupe.config import Exclusions
from loupe.exclusions import exclusion_reason
from loupe.index import upsert_excluded, upsert_item
from loupe.models import LibraryItem

COMMIT_EVERY = 500


@dataclass(frozen=True)
class ScanResult:
    scanned: int
    excluded: int


def scan_items(
    connection: sqlite3.Connection,
    items: Iterable[LibraryItem],
    exclusions: Exclusions,
    commit_every: int = COMMIT_EVERY,
) -> ScanResult:
    """Upsert every item by UUID. Re-running is safe; committed batches survive a crash."""
    scanned = 0
    excluded = 0
    for item in items:
        reason = exclusion_reason(item, exclusions)
        if reason is None:
            upsert_item(connection, item)
        else:
            upsert_excluded(connection, item.uuid, reason)
            excluded += 1
        scanned += 1
        if scanned % commit_every == 0:
            connection.commit()
    connection.commit()
    return ScanResult(scanned=scanned, excluded=excluded)
