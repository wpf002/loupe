from pathlib import Path

from loupe.config import Exclusions
from loupe.index import album_counts, connect, eligible_uuids, totals
from loupe.models import MediaType
from loupe.scan import scan_items
from tests.fixtures import UUID_A, UUID_B, UUID_C, make_item

EXCLUSIONS = Exclusions(albums=frozenset({"Private"}))


def _items() -> list:
    return [
        make_item(UUID_A, original_is_local=True),
        make_item(UUID_B, albums=("CC Travel", "Private")),
        make_item(
            UUID_C,
            media_type=MediaType.VIDEO,
            duration_seconds=12.5,
            is_screenshot=True,
            preview_path=None,
        ),
    ]


def test_excluded_items_are_not_eligible(tmp_path: Path) -> None:
    connection = connect(tmp_path / "index.sqlite")
    result = scan_items(connection, _items(), EXCLUSIONS, commit_every=1)
    assert result.scanned == 3
    assert result.excluded == 1
    assert eligible_uuids(connection) == [UUID_A, UUID_C]


def test_excluded_rows_keep_no_metadata(tmp_path: Path) -> None:
    connection = connect(tmp_path / "index.sqlite")
    scan_items(connection, _items(), EXCLUSIONS)
    row = connection.execute(
        "SELECT excluded_reason, taken_at, preview_path FROM items WHERE uuid = ?", (UUID_B,)
    ).fetchone()
    assert row == ("album", None, None)
    albums = connection.execute("SELECT COUNT(*) FROM item_albums WHERE uuid = ?", (UUID_B,))
    assert albums.fetchone()[0] == 0


def test_album_counts_skip_excluded(tmp_path: Path) -> None:
    connection = connect(tmp_path / "index.sqlite")
    scan_items(connection, _items(), EXCLUSIONS)
    assert album_counts(connection) == {"CC Travel": 2}


def test_totals(tmp_path: Path) -> None:
    connection = connect(tmp_path / "index.sqlite")
    scan_items(connection, _items(), EXCLUSIONS)
    summary = totals(connection)
    assert (summary.items, summary.eligible, summary.photos, summary.videos) == (3, 2, 1, 1)
    assert (summary.with_preview, summary.originals_local, summary.screenshots) == (1, 1, 1)
    assert summary.excluded_by_reason == {"album": 1}


def test_rescan_is_idempotent_and_applies_new_exclusions(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite"
    connection = connect(path)
    scan_items(connection, _items(), Exclusions())
    scan_items(connection, _items(), Exclusions())
    assert len(eligible_uuids(connection)) == 3
    scan_items(connection, _items(), EXCLUSIONS)
    assert eligible_uuids(connection) == [UUID_A, UUID_C]
    connection.close()
    assert eligible_uuids(connect(path)) == [UUID_A, UUID_C]


def test_unexcluding_restores_metadata(tmp_path: Path) -> None:
    connection = connect(tmp_path / "index.sqlite")
    scan_items(connection, _items(), EXCLUSIONS)
    scan_items(connection, _items(), Exclusions())
    assert album_counts(connection) == {"CC Travel": 3, "Private": 1}


def test_named_persons_are_stored_for_eligible_items_only(tmp_path: Path) -> None:
    connection = connect(tmp_path / "index.sqlite")
    items = [
        make_item(UUID_A, persons=("Test Person",)),
        make_item(UUID_B, albums=("Private",), persons=("Test Person",)),
    ]
    scan_items(connection, items, EXCLUSIONS)
    rows = connection.execute("SELECT uuid, person FROM item_persons").fetchall()
    assert rows == [(UUID_A, "Test Person")]
