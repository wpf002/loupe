"""Phone Sync: the phone's JSON list becomes the account's index, same rules as a Mac sync."""

import json
import sqlite3
from pathlib import Path

import pytest

from loupe.config import Exclusions
from loupe.sync_phone import PhoneSyncError, import_phone_items
from tests.fixtures import UUID_A, UUID_B, UUID_C


def _body(*items: dict) -> bytes:
    return json.dumps({"items": list(items)}).encode()


def _item(uuid: str, **overrides: object) -> dict:
    return {
        "uuid": uuid,
        "taken_at": "2024-05-10T12:00:00Z",
        "media_type": "photo",
        "albums": ["CC Travel"],
        **overrides,
    }


def _rows(path: Path, sql: str, parameters: tuple = ()) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()


def test_phone_items_become_eligible_rows_with_server_previews(tmp_path: Path) -> None:
    live = tmp_path / "index.sqlite"
    previews = tmp_path / "previews"
    body = _body(_item(UUID_A), _item(UUID_B, media_type="video", duration_seconds=12.5))
    assert import_phone_items(live, body, previews, Exclusions()) == 2
    assert _rows(live, "SELECT uuid, media_type FROM items ORDER BY uuid") == [
        (UUID_A, "photo"),
        (UUID_B, "video"),
    ]
    assert _rows(live, "SELECT COUNT(*) FROM item_albums WHERE album = 'CC Travel'") == [(2,)]
    expected = f"{previews.resolve()}/{UUID_A}.jpg"
    assert _rows(live, "SELECT preview_path FROM items WHERE uuid = ?", (UUID_A,)) == [(expected,)]


def test_excluded_albums_never_reach_the_index(tmp_path: Path) -> None:
    live = tmp_path / "index.sqlite"
    body = _body(_item(UUID_A), _item(UUID_C, albums=["Private"]))
    exclusions = Exclusions(albums=frozenset({"Private"}))
    assert import_phone_items(live, body, tmp_path / "previews", exclusions) == 1
    assert _rows(live, "SELECT uuid FROM items") == [(UUID_A,)]
    assert _rows(live, "SELECT COUNT(*) FROM item_albums WHERE album = 'Private'") == [(0,)]


def test_a_later_list_drops_items_the_phone_no_longer_sends(tmp_path: Path) -> None:
    live = tmp_path / "index.sqlite"
    import_phone_items(live, _body(_item(UUID_A), _item(UUID_B)), tmp_path / "p", Exclusions())
    assert import_phone_items(live, _body(_item(UUID_B)), tmp_path / "p", Exclusions()) == 1
    assert _rows(live, "SELECT uuid FROM items") == [(UUID_B,)]


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        _body(_item("not-a-uuid")),
        _body(_item(UUID_A, media_type="sticker")),
        _body(_item(UUID_A, gps="1,2")),
        _body(_item(UUID_A), _item(UUID_A)),
        _body(_item(UUID_A, duration_seconds=-1)),
    ],
)
def test_bad_uploads_are_refused(tmp_path: Path, body: bytes) -> None:
    with pytest.raises(PhoneSyncError):
        import_phone_items(tmp_path / "index.sqlite", body, tmp_path / "p", Exclusions())
