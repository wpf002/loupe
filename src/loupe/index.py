"""SQLite index keyed by Photos UUID. Every later stage reads eligible items from here."""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from loupe.models import ExclusionReason, LibraryItem

MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE items (
        uuid TEXT PRIMARY KEY,
        excluded_reason TEXT,
        taken_at TEXT,
        media_type TEXT,
        duration_seconds REAL,
        apple_score REAL,
        preview_path TEXT,
        is_screenshot INTEGER NOT NULL DEFAULT 0,
        is_screen_recording INTEGER NOT NULL DEFAULT 0,
        is_burst INTEGER NOT NULL DEFAULT 0,
        original_is_local INTEGER NOT NULL DEFAULT 0,
        scanned_at TEXT NOT NULL
    );
    CREATE TABLE item_albums (
        uuid TEXT NOT NULL REFERENCES items(uuid) ON DELETE CASCADE,
        album TEXT NOT NULL,
        PRIMARY KEY (uuid, album)
    );
    CREATE TABLE item_labels (
        uuid TEXT NOT NULL REFERENCES items(uuid) ON DELETE CASCADE,
        label TEXT NOT NULL,
        PRIMARY KEY (uuid, label)
    );
    CREATE INDEX item_albums_by_album ON item_albums(album);
    CREATE INDEX items_by_exclusion ON items(excluded_reason);
    """,
    """
    CREATE TABLE item_persons (
        uuid TEXT NOT NULL REFERENCES items(uuid) ON DELETE CASCADE,
        person TEXT NOT NULL,
        PRIMARY KEY (uuid, person)
    );
    CREATE INDEX item_persons_by_person ON item_persons(person);
    """,
    """
    CREATE TABLE embeddings (
        model TEXT NOT NULL,
        uuid TEXT NOT NULL REFERENCES items(uuid) ON DELETE CASCADE,
        vector_row INTEGER NOT NULL,
        embedded_at TEXT NOT NULL,
        PRIMARY KEY (model, uuid),
        UNIQUE (model, vector_row)
    );
    CREATE TABLE embed_skips (
        model TEXT NOT NULL,
        uuid TEXT NOT NULL REFERENCES items(uuid) ON DELETE CASCADE,
        reason TEXT NOT NULL,
        PRIMARY KEY (model, uuid)
    );
    """,
    """
    CREATE TABLE pillar_scores (
        uuid TEXT NOT NULL REFERENCES items(uuid) ON DELETE CASCADE,
        pillar TEXT NOT NULL,
        probability REAL NOT NULL,
        scored_at TEXT NOT NULL,
        PRIMARY KEY (uuid, pillar)
    );
    CREATE INDEX pillar_scores_by_pillar ON pillar_scores(pillar, probability DESC);
    """,
    """
    CREATE TABLE album_writes (
        album TEXT NOT NULL,
        uuid TEXT NOT NULL,
        written_at TEXT NOT NULL,
        PRIMARY KEY (album, uuid)
    );
    """,
    """
    CREATE TABLE transcripts (
        uuid TEXT PRIMARY KEY REFERENCES items(uuid) ON DELETE CASCADE,
        model TEXT NOT NULL,
        language TEXT,
        text TEXT NOT NULL,
        words_json TEXT NOT NULL,
        word_count INTEGER NOT NULL,
        is_talking INTEGER NOT NULL,
        transcribed_at TEXT NOT NULL
    );
    CREATE TABLE transcribe_skips (
        uuid TEXT PRIMARY KEY REFERENCES items(uuid) ON DELETE CASCADE,
        reason TEXT NOT NULL
    );
    """,
)


@dataclass(frozen=True)
class IndexTotals:
    items: int
    eligible: int
    photos: int
    videos: int
    with_preview: int
    originals_local: int
    screenshots: int
    screen_recordings: int
    bursts: int
    excluded_by_reason: dict[str, int]


def connect(index_path: Path) -> sqlite3.Connection:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(index_path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    migrate(connection)
    return connection


def migrate(connection: sqlite3.Connection) -> None:
    applied = connection.execute("PRAGMA user_version").fetchone()[0]
    for version, script in enumerate(MIGRATIONS[applied:], start=applied + 1):
        with connection:
            connection.executescript(script)
            # PRAGMA cannot take a bound parameter; version is a loop integer, not input.
            connection.execute(f"PRAGMA user_version = {int(version)}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _clear_children(connection: sqlite3.Connection, uuid: str) -> None:
    connection.execute("DELETE FROM item_albums WHERE uuid = ?", (uuid,))
    connection.execute("DELETE FROM item_labels WHERE uuid = ?", (uuid,))
    connection.execute("DELETE FROM item_persons WHERE uuid = ?", (uuid,))


def upsert_excluded(connection: sqlite3.Connection, uuid: str, reason: ExclusionReason) -> None:
    """Store only the UUID and the reason. No date, album, label, person or preview is kept."""
    _clear_children(connection, uuid)
    connection.execute(
        """
        INSERT INTO items (uuid, excluded_reason, scanned_at) VALUES (?, ?, ?)
        ON CONFLICT(uuid) DO UPDATE SET
            excluded_reason = excluded.excluded_reason,
            taken_at = NULL, media_type = NULL, duration_seconds = NULL,
            apple_score = NULL, preview_path = NULL, is_screenshot = 0,
            is_screen_recording = 0, is_burst = 0, original_is_local = 0,
            scanned_at = excluded.scanned_at
        """,
        (uuid, reason.value, _now()),
    )


def upsert_item(connection: sqlite3.Connection, item: LibraryItem) -> None:
    _clear_children(connection, item.uuid)
    connection.execute(
        """
        INSERT INTO items (
            uuid, excluded_reason, taken_at, media_type, duration_seconds, apple_score,
            preview_path, is_screenshot, is_screen_recording, is_burst, original_is_local,
            scanned_at
        ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(uuid) DO UPDATE SET
            excluded_reason = NULL,
            taken_at = excluded.taken_at,
            media_type = excluded.media_type,
            duration_seconds = excluded.duration_seconds,
            apple_score = excluded.apple_score,
            preview_path = excluded.preview_path,
            is_screenshot = excluded.is_screenshot,
            is_screen_recording = excluded.is_screen_recording,
            is_burst = excluded.is_burst,
            original_is_local = excluded.original_is_local,
            scanned_at = excluded.scanned_at
        """,
        (
            item.uuid,
            item.taken_at.isoformat() if item.taken_at else None,
            item.media_type.value,
            item.duration_seconds,
            item.apple_score,
            item.preview_path,
            int(item.is_screenshot),
            int(item.is_screen_recording),
            int(item.is_burst),
            int(item.original_is_local),
            _now(),
        ),
    )
    connection.executemany(
        "INSERT OR IGNORE INTO item_albums (uuid, album) VALUES (?, ?)",
        [(item.uuid, album) for album in item.albums],
    )
    connection.executemany(
        "INSERT OR IGNORE INTO item_labels (uuid, label) VALUES (?, ?)",
        [(item.uuid, label) for label in item.labels],
    )
    connection.executemany(
        "INSERT OR IGNORE INTO item_persons (uuid, person) VALUES (?, ?)",
        [(item.uuid, person) for person in item.persons],
    )


def eligible_uuids(connection: sqlite3.Connection) -> list[str]:
    """The only list later stages may embed, classify, search, review or write."""
    rows = connection.execute(
        "SELECT uuid FROM items WHERE excluded_reason IS NULL ORDER BY uuid"
    ).fetchall()
    return [row[0] for row in rows]


def album_counts(connection: sqlite3.Connection) -> dict[str, int]:
    rows = connection.execute(
        """
        SELECT item_albums.album, COUNT(*) FROM item_albums
        JOIN items ON items.uuid = item_albums.uuid
        WHERE items.excluded_reason IS NULL
        GROUP BY item_albums.album
        ORDER BY COUNT(*) DESC, item_albums.album
        """
    ).fetchall()
    return {album: count for album, count in rows}


def totals(connection: sqlite3.Connection) -> IndexTotals:
    row = connection.execute(
        """
        SELECT
            COUNT(*),
            COALESCE(SUM(excluded_reason IS NULL), 0),
            COALESCE(SUM(media_type = 'photo'), 0),
            COALESCE(SUM(media_type = 'video'), 0),
            COALESCE(SUM(preview_path IS NOT NULL), 0),
            COALESCE(SUM(original_is_local), 0),
            COALESCE(SUM(is_screenshot), 0),
            COALESCE(SUM(is_screen_recording), 0),
            COALESCE(SUM(is_burst), 0)
        FROM items
        """
    ).fetchone()
    reasons = connection.execute(
        """
        SELECT excluded_reason, COUNT(*) FROM items
        WHERE excluded_reason IS NOT NULL GROUP BY excluded_reason
        """
    ).fetchall()
    return IndexTotals(*row, excluded_by_reason=dict(reasons))
