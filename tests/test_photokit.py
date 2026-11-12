"""PhotoKit adapter tests. A fake Photos module stands in; the real one is never called."""

from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from loupe import photokit, previews
from loupe.models import MediaType
from loupe.photokit import (
    Access,
    AssetRecord,
    PhotoKitError,
    PhotoKitWriter,
    access_problem,
    granted_user,
    local_id_for,
    preview_ref,
    record_grant,
    to_library_item,
    uuid_from_local_id,
    uuid_from_preview_ref,
)
from loupe.previews import SkipReason, load_preview, preview_file
from tests.fixtures import UUID_A, UUID_B

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


def _record(**overrides: object) -> AssetRecord:
    fields = {
        "local_id": local_id_for(UUID_A),
        "created": NOW,
        "media_type": 1,
        "subtypes": 0,
        "duration": 0.0,
        "is_hidden": False,
        "in_burst": False,
    }
    return AssetRecord(**{**fields, **overrides})


def test_local_id_round_trip() -> None:
    assert uuid_from_local_id(local_id_for(UUID_A)) == UUID_A
    assert uuid_from_local_id(UUID_A.lower() + "/L0/001") == UUID_A
    with pytest.raises(PhotoKitError):
        uuid_from_local_id("not-a-uuid/L0/001")


def test_preview_ref_round_trip() -> None:
    assert uuid_from_preview_ref(preview_ref(UUID_A)) == UUID_A
    assert uuid_from_preview_ref("/some/file.jpeg") is None
    assert uuid_from_preview_ref("photokit:../../etc") is None


def test_photo_record_becomes_item_without_people_or_labels() -> None:
    item = to_library_item(_record(), ("CC Travel",))
    assert item.uuid == UUID_A
    assert item.media_type is MediaType.PHOTO
    assert item.duration_seconds is None
    assert item.preview_path == preview_ref(UUID_A)
    assert item.albums == ("CC Travel",)
    assert (item.persons, item.labels, item.apple_score) == ((), (), None)
    assert item.original_is_local is False


def test_flags_come_from_subtypes() -> None:
    screenshot = to_library_item(_record(subtypes=photokit.SUBTYPE_SCREENSHOT), ())
    recording = to_library_item(
        _record(media_type=2, subtypes=photokit.SUBTYPE_SCREEN_RECORDING, duration=4.5), ()
    )
    burst = to_library_item(_record(in_burst=True, is_hidden=True), ())
    assert screenshot.is_screenshot and not screenshot.is_screen_recording
    assert recording.is_screen_recording and recording.media_type is MediaType.VIDEO
    assert recording.duration_seconds == 4.5
    assert burst.is_burst and burst.is_hidden


def test_grant_is_recorded_per_macos_user(tmp_path: Path) -> None:
    assert granted_user(tmp_path) is None
    record_grant(tmp_path, "owner", NOW)
    assert granted_user(tmp_path) == "owner"
    assert access_problem(Access.AUTHORIZED, "owner", "owner") is None
    assert access_problem(Access.LIMITED, "owner", "owner") is None
    assert access_problem(Access.AUTHORIZED, "owner", "someone") is not None
    assert access_problem(Access.AUTHORIZED, None, "owner") is not None
    assert access_problem(Access.DENIED, "owner", "owner") is not None


def test_corrupt_grant_file_fails_loudly(tmp_path: Path) -> None:
    photokit.grant_path(tmp_path).write_text("{")
    with pytest.raises(PhotoKitError):
        granted_user(tmp_path)


# Previews ----------------------------------------------------------------------


def _jpeg_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (40, 30), "#336699").save(buffer, "JPEG")
    return buffer.getvalue()


@pytest.fixture
def fetched() -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []

    def fetch(uuid: str, edge: int) -> bytes | None:
        calls.append((uuid, edge))
        return _jpeg_bytes() if uuid == UUID_A else None

    previews.enable_photokit(fetch)
    yield calls
    previews.disable_photokit()


def test_photokit_ref_is_skipped_until_enabled() -> None:
    assert load_preview(preview_ref(UUID_A)) is SkipReason.NO_ACCESS


def test_photokit_ref_loads_on_demand(fetched: list[tuple[str, int]]) -> None:
    image = load_preview(preview_ref(UUID_A))
    assert isinstance(image, Image.Image)
    assert image.size == (40, 30)
    assert load_preview(preview_ref(UUID_B)) is SkipReason.MISSING_FILE
    assert fetched == [(UUID_A, photokit.EMBED_EDGE), (UUID_B, photokit.EMBED_EDGE)]


def test_file_previews_still_work(tmp_path: Path) -> None:
    path = tmp_path / "preview.jpeg"
    path.write_bytes(_jpeg_bytes())
    assert isinstance(load_preview(str(path)), Image.Image)
    assert load_preview(str(tmp_path / "gone.jpeg")) is SkipReason.MISSING_FILE
    assert load_preview(None) is SkipReason.NO_PREVIEW
    assert preview_file(str(path), tmp_path / "covers") == path


def test_cover_is_written_once(tmp_path: Path, fetched: list[tuple[str, int]]) -> None:
    covers = tmp_path / "covers"
    first = preview_file(preview_ref(UUID_A), covers)
    second = preview_file(preview_ref(UUID_A), covers)
    assert first == second == covers / f"{UUID_A}.jpg"
    assert fetched == [(UUID_A, previews.COVER_EDGE)]
    assert preview_file(preview_ref(UUID_B), covers) is None


# Album writer against a fake Photos module ---------------------------------------


class FetchResult:
    def __init__(self, items: list[object]) -> None:
        self.items = items

    def count(self) -> int:
        return len(self.items)

    def objectAtIndex_(self, position: int) -> object:  # noqa: N802 - Objective-C selector
        return self.items[position]

    def firstObject(self) -> object | None:  # noqa: N802 - Objective-C selector
        return self.items[0] if self.items else None


class Collection:
    def __init__(self, kind: str, title: str, identifier: str) -> None:
        self.kind = kind
        self.title = title
        self.identifier = identifier
        self.children: list[Collection] = []
        self.assets: list[str] = []

    def localizedTitle(self) -> str:  # noqa: N802 - Objective-C selector
        return self.title

    def localIdentifier(self) -> str:  # noqa: N802 - Objective-C selector
        return self.identifier

    def isKindOfClass_(self, cls: str) -> bool:  # noqa: N802 - Objective-C selector
        return self.kind == cls

    def placeholderForCreatedCollectionList(self) -> "Collection":  # noqa: N802
        return self

    def placeholderForCreatedAssetCollection(self) -> "Collection":  # noqa: N802
        return self

    def addChildCollections_(self, children: list["Collection"]) -> None:  # noqa: N802
        self.children.extend(children)

    def addAssets_(self, assets: FetchResult) -> None:  # noqa: N802
        self.assets.extend(str(asset) for asset in assets.items)


class FakePhotos:
    """Just enough of the Photos module for PhotoKitWriter, recording every change."""

    PHAccessLevelReadWrite = 2

    def __init__(self, top_level: list[Collection], library_ids: set[str]) -> None:
        self.top_level = top_level
        self.by_id = {collection.identifier: collection for collection in top_level}
        self.library_ids = library_ids
        self.created: list[tuple[str, str]] = []
        self.status = Access.AUTHORIZED
        fake = self
        self.PHPhotoLibrary = SimpleNamespace(
            authorizationStatusForAccessLevel_=lambda level: fake.status,
            sharedPhotoLibrary=lambda: SimpleNamespace(performChangesAndWait_error_=fake._perform),
        )
        self.PHCollection = SimpleNamespace(
            fetchTopLevelUserCollectionsWithOptions_=lambda _o: FetchResult(list(fake.top_level)),
            fetchCollectionsInCollectionList_options_=lambda f, _o: FetchResult(list(f.children)),
        )
        self.PHCollectionListChangeRequest = SimpleNamespace(
            creationRequestForCollectionListWithTitle_=lambda t: fake._create("folder", t),
            changeRequestForCollectionList_=lambda folder: folder,
        )
        self.PHAssetCollectionChangeRequest = SimpleNamespace(
            creationRequestForAssetCollectionWithTitle_=lambda t: fake._create("album", t),
            changeRequestForAssetCollection_=lambda album: album,
        )
        self.PHAsset = SimpleNamespace(
            fetchAssetsWithLocalIdentifiers_options_=lambda ids, _o: FetchResult(
                [local for local in ids if local in fake.library_ids]
            )
        )
        by_ids = SimpleNamespace(
            fetchCollectionListsWithLocalIdentifiers_options_=fake._by_ids,
            fetchAssetCollectionsWithLocalIdentifiers_options_=fake._by_ids,
        )
        self.PHCollectionList = _Kind("folder", by_ids)
        self.PHAssetCollection = _Kind("album", by_ids)

    def _perform(self, change, _error):
        change()
        return True, None

    def _create(self, kind: str, title: str) -> Collection:
        collection = Collection(kind, title, f"{kind}-{len(self.created)}")
        self.created.append((kind, title))
        self.by_id[collection.identifier] = collection
        if kind == "folder":
            self.top_level.append(collection)
        return collection

    def _by_ids(self, ids: list[str], _options: object) -> FetchResult:
        return FetchResult([self.by_id[identifier] for identifier in ids])


class _Kind(str):
    """A class token that isKindOfClass_ compares by kind, with class-level fetches."""

    def __new__(cls, kind: str, fetches: SimpleNamespace) -> "_Kind":
        token = super().__new__(cls, kind)
        token.__dict__.update(fetches.__dict__)
        return token


@pytest.fixture
def owner_grant(tmp_path: Path) -> Path:
    record_grant(tmp_path, "owner", NOW)
    return tmp_path


def test_writer_creates_folder_and_albums_only_inside_it(
    monkeypatch: pytest.MonkeyPatch, owner_grant: Path
) -> None:
    hers = Collection("album", "CC Travel", "hers-1")
    fake = FakePhotos([hers], {local_id_for(UUID_A)})
    monkeypatch.setattr(photokit, "_photos", lambda: fake)
    writer = PhotoKitWriter("Loupe", owner_grant, "owner")
    writer.ensure_album("CC Travel")
    added = writer.add("CC Travel", [UUID_A, UUID_B])
    assert added == 1
    assert fake.created == [("folder", "Loupe"), ("album", "CC Travel")]
    folder = fake.top_level[-1]
    assert [child.title for child in folder.children] == ["CC Travel"]
    assert folder.children[0].assets == [local_id_for(UUID_A)]
    assert hers.assets == []


def test_writer_reuses_existing_folder_and_album(
    monkeypatch: pytest.MonkeyPatch, owner_grant: Path
) -> None:
    folder = Collection("folder", "Loupe", "folder-x")
    folder.children.append(Collection("album", "CC Travel", "album-x"))
    fake = FakePhotos([folder], set())
    monkeypatch.setattr(photokit, "_photos", lambda: fake)
    writer = PhotoKitWriter("Loupe", owner_grant, "owner")
    writer.ensure_album("CC Travel")
    assert fake.created == []


def test_writer_refuses_other_user_or_denied_access(
    monkeypatch: pytest.MonkeyPatch, owner_grant: Path
) -> None:
    fake = FakePhotos([], set())
    monkeypatch.setattr(photokit, "_photos", lambda: fake)
    with pytest.raises(PhotoKitError):
        PhotoKitWriter("Loupe", owner_grant, "someone-else")
    fake.status = Access.DENIED
    with pytest.raises(PhotoKitError):
        PhotoKitWriter("Loupe", owner_grant, "owner")
    assert fake.created == []
