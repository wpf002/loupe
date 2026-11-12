"""M15 first run in Loupe.app: sign in, connect Photos, scan.

Photos is not read until a Loupe account is signed in, access is granted, and the
macOS user who granted it is the one running the app (CLAUDE.md rule 10). Logs
counts only. Passwords pass straight through to the Loupe server.
"""

import logging
import sqlite3
import subprocess
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path

from loupe import previews
from loupe.account import AccountError, Session, Usage
from loupe.claude import ProxyAuth, StructuredClient
from loupe.config import Exclusions
from loupe.index import connect
from loupe.models import LibraryItem
from loupe.photokit import (
    GRANTED,
    Access,
    PhotoKitError,
    access_problem,
    granted_user,
    record_grant,
)
from loupe.previews import PreviewFetcher
from loupe.scan import scan_items

PHOTOS_SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Photos"
OPEN_TIMEOUT_SECONDS = 10

logger = logging.getLogger("loupe.onboarding")


class SetupError(Exception):
    """A setup step the user can fix. The message is safe to show."""

    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class PhotosPort:
    """The PhotoKit calls setup makes. Tests pass fakes; the app passes photokit_port()."""

    status: Callable[[], Access]
    request: Callable[[], Access]
    items: Callable[[Path, str, int | None], Iterable[LibraryItem]]
    preview: PreviewFetcher
    open_settings: Callable[[], None]


def _open_photos_settings() -> None:
    subprocess.run(  # noqa: S603 - fixed argument list, no user input
        ["/usr/bin/open", PHOTOS_SETTINGS_URL], check=True, timeout=OPEN_TIMEOUT_SECONDS
    )


def photokit_port() -> PhotosPort:
    from loupe import photokit

    return PhotosPort(
        status=photokit.access_status,
        request=photokit.request_access,
        items=photokit.iter_photokit_items,
        preview=photokit.preview_jpeg,
        open_settings=_open_photos_settings,
    )


class ScanState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class ScanProgress:
    state: ScanState = ScanState.IDLE
    seen: int = 0
    scanned: int = 0
    excluded: int = 0
    error: str | None = None


def library_count(index_path: Path) -> int:
    """Read-only, so polling never races the scan thread's migration."""
    if not index_path.is_file():
        return 0
    connection = sqlite3.connect(f"{index_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM items").fetchone()[0])
    except sqlite3.OperationalError:
        # The scan has created the file but not the items table yet.
        return 0
    finally:
        connection.close()


class Onboarding:
    """Account, Photos and scan state for one macOS user. Safe to call from several threads."""

    def __init__(
        self,
        session: Session,
        photos: PhotosPort,
        data_dir: Path,
        macos_user: str,
        exclusions: Exclusions,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session = session
        self.photos = photos
        self.data_dir = data_dir
        self.macos_user = macos_user
        self.exclusions = exclusions
        self._clock = clock
        self._lock = threading.Lock()
        self._account: Usage | None = None
        self._account_checked = False
        self._progress = ScanProgress()

    @property
    def index_path(self) -> Path:
        from loupe.settings import INDEX_FILENAME

        return self.data_dir / INDEX_FILENAME

    # Account ---------------------------------------------------------------------

    def account(self) -> Usage | None:
        """The signed-in account, checked with the server once per run."""
        with self._lock:
            if self._account_checked:
                return self._account
        try:
            found = self.session.current()
        except AccountError as error:
            raise SetupError(str(error), HTTPStatus.BAD_GATEWAY) from error
        with self._lock:
            self._account, self._account_checked = found, True
        return found

    def _signed_in(self) -> Usage:
        try:
            found = self.session.current()
        except AccountError as error:
            raise SetupError(str(error), HTTPStatus.BAD_GATEWAY) from error
        if found is None:
            raise SetupError("Loupe server did not accept the new session")
        with self._lock:
            self._account, self._account_checked = found, True
        return found

    def sign_in(self, email: str, password: str) -> Usage:
        try:
            self.session.sign_in(email, password)
        except AccountError as error:
            raise SetupError(str(error)) from error
        return self._signed_in()

    def create_account(self, email: str, password: str) -> Usage:
        try:
            self.session.create_account(email, password)
        except AccountError as error:
            raise SetupError(str(error)) from error
        return self._signed_in()

    def sign_out(self) -> None:
        previews.disable_photokit()
        try:
            self.session.sign_out()
        except AccountError as error:
            raise SetupError(str(error)) from error
        finally:
            with self._lock:
                self._account, self._account_checked = None, True

    def _require_account(self) -> None:
        if self.account() is None:
            raise SetupError("Sign in first", HTTPStatus.UNAUTHORIZED)

    # Photos ----------------------------------------------------------------------

    def photos_access(self) -> Access:
        return self.photos.status()

    def photos_problem(self) -> str | None:
        return access_problem(self.photos.status(), granted_user(self.data_dir), self.macos_user)

    def connect_photos(self) -> Access:
        """Shows the macOS prompt when it has not been answered, then records who granted it."""
        self._require_account()
        access = self.photos.status()
        if access is Access.NOT_DETERMINED:
            try:
                access = self.photos.request()
            except PhotoKitError as error:
                raise SetupError(str(error)) from error
        if access in GRANTED and granted_user(self.data_dir) is None:
            record_grant(self.data_dir, self.macos_user, self._clock())
            logger.info("photos access recorded")
        problem = access_problem(access, granted_user(self.data_dir), self.macos_user)
        if problem is not None:
            raise SetupError(problem, HTTPStatus.FORBIDDEN)
        previews.enable_photokit(self.photos.preview)
        return access

    def open_photos_settings(self) -> None:
        try:
            self.photos.open_settings()
        except (OSError, subprocess.SubprocessError) as error:
            raise SetupError("Could not open System Settings") from error

    def photos_ready(self) -> bool:
        """True when Photos may be read now. Turns on PhotoKit previews when it is."""
        if self.account() is None or self.photos_problem() is not None:
            previews.disable_photokit()
            return False
        previews.enable_photokit(self.photos.preview)
        return True

    def _require_photos(self) -> None:
        self._require_account()
        problem = self.photos_problem()
        if problem is not None:
            raise SetupError(problem, HTTPStatus.FORBIDDEN)

    # Scan ------------------------------------------------------------------------

    def progress(self) -> ScanProgress:
        with self._lock:
            return self._progress

    def _update(self, **changes: object) -> None:
        with self._lock:
            self._progress = replace(self._progress, **changes)

    def start_scan(self, limit: int | None = None) -> threading.Thread:
        self._require_photos()
        with self._lock:
            if self._progress.state is ScanState.RUNNING:
                raise SetupError("A scan is running", HTTPStatus.CONFLICT)
            self._progress = ScanProgress(state=ScanState.RUNNING)
            # Migrate here, once, so the scan thread and status reads never both run it.
            connect(self.index_path).close()
        thread = threading.Thread(target=self._scan, args=(limit,), name="loupe-scan", daemon=True)
        thread.start()
        return thread

    def _counted(self, items: Iterable[LibraryItem]) -> Iterator[LibraryItem]:
        for seen, item in enumerate(items, start=1):
            self._update(seen=seen)
            yield item

    def _scan(self, limit: int | None) -> None:
        try:
            connection = connect(self.index_path)
            try:
                items = self.photos.items(self.data_dir, self.macos_user, limit)
                result = scan_items(connection, self._counted(items), self.exclusions)
            finally:
                connection.close()
        except PhotoKitError as error:
            self._update(state=ScanState.FAILED, error=str(error))
            logger.warning("scan stopped: photos access")
            return
        except Exception:
            self._update(state=ScanState.FAILED, error="Scan failed")
            logger.exception("scan failed")
            return
        self._update(state=ScanState.DONE, scanned=result.scanned, excluded=result.excluded)
        logger.info("scan done: %d scanned, %d excluded", result.scanned, result.excluded)

    # Summary ---------------------------------------------------------------------

    def ready(self) -> bool:
        return self.photos_ready() and library_count(self.index_path) > 0

    def summary(self) -> dict[str, object]:
        account = self.account()
        signed_in = account is not None
        access = self.photos_access()
        photos_ok = signed_in and self.photos_ready()
        progress = self.progress()
        count = library_count(self.index_path)
        return {
            "mode": "app",
            "account": account.model_dump() if account else None,
            "photos": {"access": access.name.lower(), "ready": photos_ok},
            "library": {
                "items": count,
                "scan": {
                    "state": progress.state.value,
                    "seen": progress.seen,
                    "scanned": progress.scanned,
                    "excluded": progress.excluded,
                    "error": progress.error,
                },
            },
            "ready": photos_ok and count > 0 and progress.state is not ScanState.RUNNING,
        }


def proxy_client_factory(server_url: str, session: Session) -> Callable[[], StructuredClient]:
    """Claude calls in the app go through the Loupe server with the session token."""
    from loupe.studio import StudioError, claude_client

    def build() -> StructuredClient:
        token = session.token()
        if token is None:
            raise StudioError("Sign in to use Claude")
        return claude_client(ProxyAuth(server_url, token))

    return build
