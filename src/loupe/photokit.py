"""PhotoKit adapter for Loupe.app (checked against pyobjc-framework-Photos 13.0).

Reads the library and adds items to albums inside the Loupe folder. Nothing here
deletes, edits, moves, hides or re-titles an item or an album the owner made.

Every entry point calls `require_access` first: the macOS permission must be
granted, and the grant must have been recorded by the macOS user running now.
The Photos framework is imported inside functions so tests and the CLI never
load it.
"""

import json
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
from typing import Any

from loupe.models import LibraryItem, MediaType
from loupe.names import validate_album_name

PREVIEW_SCHEME = "photokit:"
# PhotoKit local identifiers for library assets are the UUID plus this suffix.
LOCAL_ID_SUFFIX = "/L0/001"
GRANT_FILENAME = "photokit-grant.json"
EMBED_EDGE = 512
JPEG_QUALITY = 0.85
ACCESS_PROMPT_TIMEOUT_SECONDS = 600.0

# Values of PHAuthorizationStatus and the PHAssetMediaType / subtype bit masks.
SUBTYPE_SCREENSHOT = 1 << 2
SUBTYPE_SCREEN_RECORDING = 1 << 19
MEDIA_TYPE_VIDEO = 2

_UUID_PATTERN = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")


class Access(IntEnum):
    NOT_DETERMINED = 0
    RESTRICTED = 1
    DENIED = 2
    AUTHORIZED = 3
    LIMITED = 4


GRANTED = frozenset({Access.AUTHORIZED, Access.LIMITED})


class PhotoKitError(Exception):
    """Raised when Photos refuses a request or access is not in place."""


@dataclass(frozen=True)
class AssetRecord:
    """The PHAsset fields Loupe reads, copied out of Objective-C."""

    local_id: str
    created: datetime | None
    media_type: int
    subtypes: int
    duration: float
    is_hidden: bool
    in_burst: bool


def uuid_from_local_id(local_id: str) -> str:
    uuid = local_id.split("/", 1)[0].upper()
    if not _UUID_PATTERN.match(uuid):
        raise PhotoKitError("PhotoKit returned an identifier that is not a UUID")
    return uuid


def local_id_for(uuid: str) -> str:
    return uuid + LOCAL_ID_SUFFIX


def preview_ref(uuid: str) -> str:
    return PREVIEW_SCHEME + uuid


def uuid_from_preview_ref(preview_path: str) -> str | None:
    """The UUID behind a 'photokit:' preview reference, or None for a file path."""
    if not preview_path.startswith(PREVIEW_SCHEME):
        return None
    uuid = preview_path[len(PREVIEW_SCHEME) :]
    return uuid if _UUID_PATTERN.match(uuid) else None


def to_library_item(record: AssetRecord, albums: tuple[str, ...]) -> LibraryItem:
    """PhotoKit has no person names, labels or quality score. Those stay empty."""
    uuid = uuid_from_local_id(record.local_id)
    is_video = record.media_type == MEDIA_TYPE_VIDEO
    return LibraryItem(
        uuid=uuid,
        taken_at=record.created,
        media_type=MediaType.VIDEO if is_video else MediaType.PHOTO,
        duration_seconds=record.duration if is_video else None,
        apple_score=None,
        preview_path=preview_ref(uuid),
        albums=albums,
        labels=(),
        persons=(),
        is_hidden=record.is_hidden,
        is_screenshot=bool(record.subtypes & SUBTYPE_SCREENSHOT),
        is_screen_recording=bool(record.subtypes & SUBTYPE_SCREEN_RECORDING),
        is_burst=record.in_burst,
        original_is_local=False,
    )


# Grant record ------------------------------------------------------------------


def grant_path(data_dir: Path) -> Path:
    return data_dir / GRANT_FILENAME


def record_grant(data_dir: Path, macos_user: str, now: datetime) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    grant = {"macos_user": macos_user, "granted_at": now.isoformat()}
    grant_path(data_dir).write_text(json.dumps(grant))


def granted_user(data_dir: Path) -> str | None:
    path = grant_path(data_dir)
    if not path.is_file():
        return None
    try:
        grant = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise PhotoKitError(f"{GRANT_FILENAME} is not valid JSON") from error
    user = grant.get("macos_user") if isinstance(grant, dict) else None
    return user if isinstance(user, str) and user else None


def access_problem(access: Access, recorded_user: str | None, current_user: str) -> str | None:
    """Why Loupe may not read Photos now, or None when it may."""
    if access not in GRANTED:
        return "Photos access is not granted"
    if recorded_user is None:
        return "Photos access was not granted through Loupe"
    if recorded_user != current_user:
        return f"Photos access was granted by '{recorded_user}', not '{current_user}'"
    return None


# Photos framework calls --------------------------------------------------------


def _photos() -> Any:
    import Photos

    return Photos


def access_status() -> Access:
    photos = _photos()
    return Access(
        photos.PHPhotoLibrary.authorizationStatusForAccessLevel_(photos.PHAccessLevelReadWrite)
    )


def request_access(timeout: float = ACCESS_PROMPT_TIMEOUT_SECONDS) -> Access:
    """Shows the macOS 'Allow access to photos' prompt and waits for the answer."""
    photos = _photos()
    answered = threading.Event()
    result: dict[str, Access] = {}

    def handler(status: int) -> None:
        result["status"] = Access(status)
        answered.set()

    photos.PHPhotoLibrary.requestAuthorizationForAccessLevel_handler_(
        photos.PHAccessLevelReadWrite, handler
    )
    if not answered.wait(timeout):
        raise PhotoKitError("no answer to the Photos access prompt")
    return result["status"]


def require_access(data_dir: Path, current_user: str) -> None:
    problem = access_problem(access_status(), granted_user(data_dir), current_user)
    if problem is not None:
        raise PhotoKitError(problem)


def _to_datetime(nsdate: Any) -> datetime | None:
    if nsdate is None:
        return None
    return datetime.fromtimestamp(nsdate.timeIntervalSince1970(), UTC)


def _asset_record(asset: Any) -> AssetRecord:
    return AssetRecord(
        local_id=str(asset.localIdentifier()),
        created=_to_datetime(asset.creationDate()),
        media_type=int(asset.mediaType()),
        subtypes=int(asset.mediaSubtypes()),
        duration=float(asset.duration()),
        is_hidden=bool(asset.isHidden()),
        in_burst=asset.burstIdentifier() is not None,
    )


def _each(fetch_result: Any) -> Iterator[Any]:
    for position in range(fetch_result.count()):
        yield fetch_result.objectAtIndex_(position)


def _album_members(photos: Any) -> dict[str, tuple[str, ...]]:
    """UUID to the titles of the user albums it is in, folders included."""
    members: dict[str, list[str]] = {}
    albums = photos.PHAssetCollection.fetchAssetCollectionsWithType_subtype_options_(
        photos.PHAssetCollectionTypeAlbum, photos.PHAssetCollectionSubtypeAlbumRegular, None
    )
    for album in _each(albums):
        title = album.localizedTitle()
        if title is None:
            continue
        assets = photos.PHAsset.fetchAssetsInAssetCollection_options_(album, None)
        for asset in _each(assets):
            uuid = uuid_from_local_id(str(asset.localIdentifier()))
            members.setdefault(uuid, []).append(str(title))
    return {uuid: tuple(titles) for uuid, titles in members.items()}


def iter_photokit_items(
    data_dir: Path, current_user: str, limit: int | None
) -> Iterator[LibraryItem]:
    """Every item, in UUID order so --limit picks the same slice each run.

    Hidden items are fetched so scan can store them as UUID plus reason.
    Recently deleted items are never in a PhotoKit fetch.
    """
    require_access(data_dir, current_user)
    photos = _photos()
    albums = _album_members(photos)
    options = photos.PHFetchOptions.alloc().init()
    options.setIncludeHiddenAssets_(True)
    assets = photos.PHAsset.fetchAssetsWithOptions_(options)
    records = [_asset_record(asset) for asset in _each(assets)]
    records.sort(key=lambda record: record.local_id)
    selected = records if limit is None else records[:limit]
    for record in selected:
        yield to_library_item(record, albums.get(uuid_from_local_id(record.local_id), ()))


def preview_jpeg(uuid: str, edge: int = EMBED_EDGE) -> bytes | None:
    """A JPEG at most `edge` pixels on its long side, pulled from Photos on demand.

    Photos may fetch a sized derivative from iCloud. It never downloads the original.
    Access is checked by the caller that read the UUID from the index.
    """
    photos = _photos()
    import AppKit

    assets = photos.PHAsset.fetchAssetsWithLocalIdentifiers_options_([local_id_for(uuid)], None)
    if assets.count() == 0:
        return None
    options = photos.PHImageRequestOptions.alloc().init()
    options.setSynchronous_(True)
    options.setDeliveryMode_(photos.PHImageRequestOptionsDeliveryModeHighQualityFormat)
    options.setResizeMode_(photos.PHImageRequestOptionsResizeModeFast)
    options.setNetworkAccessAllowed_(True)
    found: dict[str, Any] = {}

    def handler(image: Any, _info: Any) -> None:
        found["image"] = image

    manager = photos.PHImageManager.defaultManager()
    manager.requestImageForAsset_targetSize_contentMode_options_resultHandler_(
        assets.firstObject(), (edge, edge), photos.PHImageContentModeAspectFit, options, handler
    )
    image = found.get("image")
    if image is None:
        return None
    bitmap = AppKit.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
    data = bitmap.representationUsingType_properties_(
        AppKit.NSBitmapImageFileTypeJPEG, {AppKit.NSImageCompressionFactor: JPEG_QUALITY}
    )
    return bytes(data) if data is not None else None


class PhotoKitWriter:
    """Adds items to albums inside one top-level folder that Loupe creates and owns."""

    def __init__(self, folder_name: str, data_dir: Path, current_user: str) -> None:
        require_access(data_dir, current_user)
        self._photos = _photos()
        self._library = self._photos.PHPhotoLibrary.sharedPhotoLibrary()
        self._folder_name = validate_album_name(folder_name)
        self._folder = self._find_folder() or self._create_folder()
        self._albums: dict[str, Any] = {}

    def _perform(self, change: Callable[[], None]) -> None:
        ok, error = self._library.performChangesAndWait_error_(change, None)
        if not ok:
            code = error.code() if error is not None else "unknown"
            raise PhotoKitError(f"Photos refused the change (code {code})")

    def _find_folder(self) -> Any | None:
        top_level = self._photos.PHCollection.fetchTopLevelUserCollectionsWithOptions_(None)
        for collection in _each(top_level):
            is_folder = collection.isKindOfClass_(self._photos.PHCollectionList)
            if is_folder and collection.localizedTitle() == self._folder_name:
                return collection
        return None

    def _create_folder(self) -> Any:
        created: dict[str, str] = {}

        def change() -> None:
            lists = self._photos.PHCollectionListChangeRequest
            request = lists.creationRequestForCollectionListWithTitle_(self._folder_name)
            created["id"] = request.placeholderForCreatedCollectionList().localIdentifier()

        self._perform(change)
        fetched = self._photos.PHCollectionList.fetchCollectionListsWithLocalIdentifiers_options_(
            [created["id"]], None
        )
        return fetched.firstObject()

    def _album_in_folder(self, album: str) -> Any | None:
        children = self._photos.PHCollection.fetchCollectionsInCollectionList_options_(
            self._folder, None
        )
        for collection in _each(children):
            is_album = collection.isKindOfClass_(self._photos.PHAssetCollection)
            if is_album and collection.localizedTitle() == album:
                return collection
        return None

    def _create_album(self, album: str) -> Any:
        created: dict[str, str] = {}

        def change() -> None:
            albums = self._photos.PHAssetCollectionChangeRequest
            request = albums.creationRequestForAssetCollectionWithTitle_(album)
            placeholder = request.placeholderForCreatedAssetCollection()
            created["id"] = placeholder.localIdentifier()
            lists = self._photos.PHCollectionListChangeRequest
            folder_request = lists.changeRequestForCollectionList_(self._folder)
            folder_request.addChildCollections_([placeholder])

        self._perform(change)
        fetched = self._photos.PHAssetCollection.fetchAssetCollectionsWithLocalIdentifiers_options_(
            [created["id"]], None
        )
        return fetched.firstObject()

    def ensure_album(self, album: str) -> None:
        album = validate_album_name(album)
        if album in self._albums:
            return
        self._albums[album] = self._album_in_folder(album) or self._create_album(album)

    def add(self, album: str, uuids: list[str]) -> int:
        target = self._albums[album]
        assets = self._photos.PHAsset.fetchAssetsWithLocalIdentifiers_options_(
            [local_id_for(uuid) for uuid in uuids], None
        )

        def change() -> None:
            request = self._photos.PHAssetCollectionChangeRequest.changeRequestForAssetCollection_(
                target
            )
            request.addAssets_(assets)

        self._perform(change)
        return int(assets.count())
