from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from loupe.library import UNNAMED_PERSON, build_preview_index, named_persons, to_library_item
from loupe.models import MediaType
from tests.fixtures import UUID_A, UUID_B


def _fake_photo(**overrides: object) -> SimpleNamespace:
    values = {
        "uuid": UUID_A,
        "date": datetime(2024, 1, 2, tzinfo=UTC),
        "ismovie": False,
        "exif_info": None,
        "score": SimpleNamespace(overall=0.25),
        "albums": ["CC Travel"],
        "labels": ["Beach"],
        "persons": [],
        "hidden": False,
        "screenshot": False,
        "screen_recording": False,
        "burst": False,
        "ismissing": True,
        "path": None,
        "shared": False,
        "path_derivatives": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _touch(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_preview_index_picks_largest_image(tmp_path: Path) -> None:
    derivatives = tmp_path / "resources/derivatives"
    _touch(derivatives / "0" / f"{UUID_A}_1_105_c.jpeg", 10)
    _touch(derivatives / "0" / f"{UUID_A}_1_201_a.jpeg", 50)
    _touch(derivatives / "masters/0" / f"{UUID_A}_4_5005_c.jpeg", 5)
    _touch(derivatives / "0" / f"{UUID_B}_2_0_a.mov", 500)
    _touch(derivatives / "0" / f"{UUID_B}_1_201_a.THM", 5)
    index = build_preview_index(tmp_path)
    assert index == {UUID_A: str(derivatives / "0" / f"{UUID_A}_1_201_a.jpeg")}


def test_cloud_only_photo_with_preview() -> None:
    item = to_library_item(_fake_photo(), {UUID_A: "/synthetic/a.jpeg"})
    assert item.preview_path == "/synthetic/a.jpeg"
    assert item.original_is_local is False
    assert item.media_type is MediaType.PHOTO
    assert item.apple_score == 0.25


def test_local_video_with_duration() -> None:
    photo = _fake_photo(
        ismovie=True,
        exif_info=SimpleNamespace(duration=31.5),
        ismissing=False,
        path="/synthetic/a.mov",
        score=None,
    )
    item = to_library_item(photo, {})
    assert item.media_type is MediaType.VIDEO
    assert item.duration_seconds == 31.5
    assert item.original_is_local is True
    assert item.apple_score is None
    assert item.preview_path is None


def test_unnamed_faces_are_dropped() -> None:
    assert named_persons(["Test Person", UNNAMED_PERSON, None]) == ("Test Person",)
