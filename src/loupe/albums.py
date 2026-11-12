"""M5: put confident picks into albums inside the Loupe folder. Add only, never remove."""

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import batched
from typing import Protocol

from loupe.classify import picks
from loupe.config import PillarConfig
from loupe.names import validate_album_name

ADD_BATCH = 200
PILLAR_PREFIX_SEPARATOR = " "


class AlbumWriter(Protocol):
    """Everything Loupe may do to Photos. Nothing here deletes, edits or moves."""

    def ensure_album(self, album: str) -> None: ...

    def add(self, album: str, uuids: list[str]) -> int: ...


@dataclass(frozen=True)
class AlbumPlan:
    album: str
    pillar: str
    picks: int
    already_written: int
    to_add: list[str]


def album_name(pillar: str, config: PillarConfig) -> str:
    """'CC Travel' becomes 'Travel'; albums from include_albums keep their name."""
    for prefix in config.label_album_prefixes:
        if pillar.startswith(prefix) and pillar[len(prefix) :].strip():
            return validate_album_name(pillar[len(prefix) :])
    return validate_album_name(pillar)


def _written(connection: sqlite3.Connection, album: str) -> set[str]:
    rows = connection.execute("SELECT uuid FROM album_writes WHERE album = ?", (album,))
    return {row[0] for row in rows}


def plan_albums(
    connection: sqlite3.Connection,
    pillars: tuple[str, ...],
    config: PillarConfig,
    limit: int | None = None,
) -> list[AlbumPlan]:
    plans = []
    for pillar in pillars:
        album = album_name(pillar, config)
        uuids = [pick.uuid for pick in picks(connection, pillar, config.min_confidence, 1.01, None)]
        written = _written(connection, album)
        to_add = [uuid for uuid in uuids if uuid not in written]
        if limit is not None:
            to_add = to_add[:limit]
        plans.append(AlbumPlan(album, pillar, len(uuids), len(written), to_add))
    return plans


def _record(connection: sqlite3.Connection, album: str, uuids: list[str]) -> None:
    now = datetime.now(UTC).isoformat()
    with connection:
        connection.executemany(
            "INSERT OR IGNORE INTO album_writes (album, uuid, written_at) VALUES (?, ?, ?)",
            [(album, uuid, now) for uuid in uuids],
        )


def apply_plans(
    connection: sqlite3.Connection, plans: list[AlbumPlan], writer: AlbumWriter
) -> Iterator[tuple[str, int]]:
    """Yields (album, added) after every batch, so a crash resumes at the next batch."""
    for plan in plans:
        if not plan.to_add:
            continue
        writer.ensure_album(plan.album)
        for batch in batched(plan.to_add, ADD_BATCH):
            added = writer.add(plan.album, list(batch))
            _record(connection, plan.album, list(batch))
            yield plan.album, added


class PhotoscriptWriter:
    """Writes through Photos' AppleScript interface, only inside one top-level folder."""

    def __init__(self, folder_name: str) -> None:
        import photoscript

        self._photoscript = photoscript
        self._library = photoscript.PhotosLibrary()
        folder_name = validate_album_name(folder_name)
        folder = self._library.folder(name=folder_name, top_level=True)
        self._folder = folder or self._library.create_folder(folder_name)
        self._albums: dict[str, object] = {}

    def ensure_album(self, album: str) -> None:
        if album in self._albums:
            return
        existing = self._folder.album(album)
        self._albums[album] = existing or self._folder.create_album(album)

    def add(self, album: str, uuids: list[str]) -> int:
        photos = [_PhotoRef(uuid + self._photoscript.UUID_SUFFIX_PHOTO) for uuid in uuids]
        return len(self._albums[album].add(photos))  # type: ignore[attr-defined]


@dataclass(frozen=True)
class _PhotoRef:
    """Album.add only reads .id; photoscript.Photo(uuid) costs one AppleScript call each."""

    id: str
