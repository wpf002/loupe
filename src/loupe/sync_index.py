"""Copies the eligible part of an index between SQLite files, for Sync.

The Mac builds an upload with `export_index`: eligible items only, no person names,
no embeddings, no excluded UUIDs. The web server applies it with `import_index`, which
checks the file, then replaces the library rows on the live index in one transaction.
Hooks, weeks and emails already on the server are kept, since the web app makes them too.
"""

import sqlite3
from pathlib import Path

from loupe.index import MIGRATIONS, connect

INTEGRITY_OK = "ok"
# Attached schema names. SQL below interpolates only these constants, never input.
SOURCE_SCHEMAS = frozenset({"up", "src"})
ITEM_COLUMNS = (
    "uuid",
    "excluded_reason",
    "taken_at",
    "media_type",
    "duration_seconds",
    "apple_score",
    "is_screenshot",
    "is_screen_recording",
    "is_burst",
    "original_is_local",
    "scanned_at",
)
# Rows keyed by an item UUID that Sync replaces. item_persons and embeddings never travel.
CHILD_TABLES: dict[str, tuple[str, ...]] = {
    "item_albums": ("uuid", "album"),
    "item_labels": ("uuid", "label"),
    "pillar_scores": ("uuid", "pillar", "probability", "scored_at"),
    "album_writes": ("album", "uuid", "written_at"),
    "transcripts": (
        "uuid",
        "model",
        "language",
        "text",
        "words_json",
        "word_count",
        "is_talking",
        "transcribed_at",
    ),
}
CLEARED_TABLES = (*CHILD_TABLES, "item_persons", "transcribe_skips", "embed_skips")
# Made by either side. Rows already in the target win.
GENERATED_TABLES: dict[str, tuple[str, ...]] = {
    "hooks": ("uuid", "model", "hooks_json", "created_at"),
    "weeks": ("week_start", "model", "plan_json", "assets_json", "created_at"),
    "emails": ("week_start", "model", "draft_json", "created_at"),
}
REQUIRED_TABLES = ("items", *CHILD_TABLES, *GENERATED_TABLES)


class SyncIndexError(Exception):
    """The uploaded index can't be applied. The message is safe to show."""


def _schema(name: str) -> str:
    if name not in SOURCE_SCHEMAS:
        raise ValueError(f"unknown schema {name!r}")
    return name


def copy_eligible(connection: sqlite3.Connection, schema: str, preview_prefix: str) -> None:
    """Make main's library rows match the eligible items in `schema`.

    preview_path becomes preview_prefix + uuid + ".jpg", or NULL when the prefix is empty.
    The caller owns the transaction.
    """
    source = _schema(schema)
    columns = ", ".join(ITEM_COLUMNS)
    updates = ", ".join(f"{name} = excluded.{name}" for name in (*ITEM_COLUMNS[1:], "preview_path"))
    connection.execute("CREATE TEMP TABLE IF NOT EXISTS keep (uuid TEXT PRIMARY KEY)")
    connection.execute("DELETE FROM temp.keep")
    connection.execute(
        f"INSERT INTO temp.keep SELECT uuid FROM {source}.items "  # noqa: S608 constant schema
        "WHERE excluded_reason IS NULL"
    )
    for table in CLEARED_TABLES:
        connection.execute(f"DELETE FROM main.{table}")  # noqa: S608 constant table name
    connection.execute("DELETE FROM main.items WHERE uuid NOT IN (SELECT uuid FROM temp.keep)")
    connection.execute(
        f"INSERT INTO main.items ({columns}, preview_path) "  # noqa: S608 constant names
        f"SELECT {columns}, NULLIF(?, '') || uuid || '.jpg' FROM {source}.items "
        f"WHERE uuid IN (SELECT uuid FROM temp.keep) "
        f"ON CONFLICT(uuid) DO UPDATE SET {updates}",
        (preview_prefix,),
    )
    for table, names in CHILD_TABLES.items():
        listed = ", ".join(names)
        connection.execute(
            f"INSERT OR REPLACE INTO main.{table} ({listed}) "  # noqa: S608 constant names
            f"SELECT {listed} FROM {source}.{table} WHERE uuid IN (SELECT uuid FROM temp.keep)"
        )
    for table, names in GENERATED_TABLES.items():
        listed = ", ".join(names)
        where = " WHERE uuid IN (SELECT uuid FROM temp.keep)" if "uuid" in names else ""
        connection.execute(
            f"INSERT OR IGNORE INTO main.{table} ({listed}) "  # noqa: S608 constant names
            f"SELECT {listed} FROM {source}.{table}{where}"
        )
    connection.execute("DROP TABLE temp.keep")


def _harden(connection: sqlite3.Connection) -> None:
    """An uploaded file is untrusted: no schema-defined functions, no writes to its schema."""
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
    connection.execute("PRAGMA trusted_schema = OFF")


def _check_upload(connection: sqlite3.Connection, schema: str) -> None:
    source = _schema(schema)
    check = connection.execute(f"PRAGMA {source}.integrity_check").fetchone()
    if check is None or check[0] != INTEGRITY_OK:
        raise SyncIndexError("The uploaded library failed its integrity check")
    version = connection.execute(f"PRAGMA {source}.user_version").fetchone()[0]
    if version != len(MIGRATIONS):
        raise SyncIndexError("Update Loupe.app, then sync again")
    found = {
        row[0]
        for row in connection.execute(
            f"SELECT name FROM {source}.sqlite_master WHERE type = 'table'"  # noqa: S608
        )
    }
    missing = [name for name in REQUIRED_TABLES if name not in found]
    if missing:
        raise SyncIndexError("The uploaded library is missing tables")


def export_index(source_path: Path, target_path: Path) -> int:
    """Write the upload file. Returns the number of eligible items in it."""
    target_path.unlink(missing_ok=True)
    connection = connect(target_path)
    try:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("ATTACH DATABASE ? AS src", (str(source_path),))
        with connection:
            copy_eligible(connection, "src", "")
        connection.execute("DETACH DATABASE src")
        return int(connection.execute("SELECT COUNT(*) FROM items").fetchone()[0])
    finally:
        connection.close()


def import_index(index_path: Path, upload_path: Path, previews_dir: Path) -> int:
    """Apply an uploaded index to the live one. Returns the eligible item count."""
    connection = connect(index_path)
    try:
        _harden(connection)
        try:
            connection.execute("ATTACH DATABASE ? AS up", (str(upload_path),))
        except sqlite3.DatabaseError as error:
            raise SyncIndexError("The uploaded library could not be read") from error
        try:
            _check_upload(connection, "up")
            prefix = f"{previews_dir.resolve()}/"
            connection.execute("BEGIN IMMEDIATE")
            try:
                copy_eligible(connection, "up", prefix)
            except BaseException:
                connection.rollback()
                raise
            connection.commit()
        except sqlite3.DatabaseError as error:
            raise SyncIndexError("The uploaded library could not be read") from error
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.execute("DETACH DATABASE up")
        return int(connection.execute("SELECT COUNT(*) FROM items").fetchone()[0])
    finally:
        connection.close()
