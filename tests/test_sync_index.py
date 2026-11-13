"""Index Sync: eligible rows only leave the Mac, and the server keeps what it made."""

import gzip
import sqlite3
from pathlib import Path

import pytest

from loupe.config import Exclusions
from loupe.hooks import load_hooks, save_hooks
from loupe.index import connect
from loupe.scan import scan_items
from loupe.sync_index import SyncIndexError, export_index, import_index
from tests.fixtures import UUID_A, UUID_B, UUID_C, make_item


def _mac_index(tmp_path: Path) -> Path:
    path = tmp_path / "mac.sqlite"
    connection = connect(path)
    items = [
        make_item(UUID_A, persons=("Test Person",)),
        make_item(UUID_B),
        make_item(UUID_C, albums=("Private",)),
    ]
    scan_items(connection, items, Exclusions(albums=frozenset({"Private"})))
    connection.close()
    return path


def _rows(path: Path, sql: str, parameters: tuple = ()) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()


def test_export_drops_excluded_items_and_person_names(tmp_path: Path) -> None:
    upload = tmp_path / "upload.sqlite"
    assert export_index(_mac_index(tmp_path), upload) == 2
    assert _rows(upload, "SELECT uuid FROM items ORDER BY uuid") == [(UUID_A,), (UUID_B,)]
    assert _rows(upload, "SELECT COUNT(*) FROM item_persons") == [(0,)]
    assert _rows(upload, "SELECT DISTINCT preview_path FROM items") == [(None,)]
    assert _rows(upload, "SELECT COUNT(*) FROM item_albums") == [(2,)]


def test_import_points_previews_at_the_server_and_keeps_its_hooks(tmp_path: Path) -> None:
    upload = tmp_path / "upload.sqlite"
    export_index(_mac_index(tmp_path), upload)
    live = tmp_path / "live.sqlite"
    previews = tmp_path / "previews"
    assert import_index(live, upload, previews) == 2
    connection = connect(live)
    save_hooks(connection, UUID_A, ("One", "Two"))
    connection.commit()
    connection.close()
    assert import_index(live, upload, previews) == 2
    connection = connect(live)
    assert load_hooks(connection, UUID_A) == ("One", "Two")
    connection.close()
    expected = f"{previews.resolve()}/{UUID_A}.jpg"
    assert _rows(live, "SELECT preview_path FROM items WHERE uuid = ?", (UUID_A,)) == [(expected,)]


def test_import_removes_items_no_longer_eligible(tmp_path: Path) -> None:
    live = tmp_path / "live.sqlite"
    first = tmp_path / "first.sqlite"
    export_index(_mac_index(tmp_path), first)
    import_index(live, first, tmp_path / "previews")
    mac = connect(tmp_path / "mac.sqlite")
    scan_items(
        mac, [make_item(UUID_B, albums=("Private",))], Exclusions(albums=frozenset({"Private"}))
    )
    mac.close()
    second = tmp_path / "second.sqlite"
    export_index(tmp_path / "mac.sqlite", second)
    assert import_index(live, second, tmp_path / "previews") == 1
    assert _rows(live, "SELECT uuid FROM items") == [(UUID_A,)]


def test_import_rejects_an_old_schema(tmp_path: Path) -> None:
    upload = tmp_path / "upload.sqlite"
    export_index(_mac_index(tmp_path), upload)
    connection = sqlite3.connect(upload)
    connection.execute("PRAGMA user_version = 1")
    connection.close()
    with pytest.raises(SyncIndexError, match="Update Loupe.app"):
        import_index(tmp_path / "live.sqlite", upload, tmp_path / "previews")


def test_import_rejects_a_file_that_is_not_sqlite(tmp_path: Path) -> None:
    upload = tmp_path / "upload.sqlite"
    upload.write_bytes(gzip.compress(b"not a database"))
    with pytest.raises(SyncIndexError):
        import_index(tmp_path / "live.sqlite", upload, tmp_path / "previews")
