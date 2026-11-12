"""Loupe.app first run: sign in, connect Photos, scan. Fakes stand in for PhotoKit."""

import http.client
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loupe import previews
from loupe.account import AccountError, Usage
from loupe.config import Exclusions
from loupe.onboarding import Onboarding, PhotosPort, ScanState, SetupError, proxy_client_factory
from loupe.photokit import Access, granted_user, preview_ref, record_grant
from loupe.previews import SkipReason, load_preview
from loupe.server import TOKEN_HEADER, App, make_server
from loupe.settings import Settings
from loupe.studio import Studio, StudioError
from tests.fixtures import UUID_A, UUID_B, UUID_C, make_brand, make_item
from tests.test_server import _free_port

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
USER = "owner"
EMAIL = "owner@example.test"
PASSWORD = "correct horse battery"  # noqa: S105 - test value
USAGE = Usage(email=EMAIL, month="2026-11", used=0, cap=2_000_000)


class FakeSession:
    def __init__(self) -> None:
        self.stored: str | None = None
        self.server_down = False

    def token(self) -> str | None:
        return self.stored

    def sign_in(self, email: str, password: str) -> str:
        if password != PASSWORD:
            raise AccountError("Wrong email or password", 401)
        self.stored = "session-token"
        return email

    def create_account(self, email: str, password: str) -> str:
        return self.sign_in(email, password)

    def sign_out(self) -> None:
        self.stored = None

    def current(self) -> Usage | None:
        if self.server_down:
            raise AccountError("could not reach the Loupe server")
        return USAGE if self.stored else None


class FakePhotos:
    def __init__(self, answer: Access = Access.AUTHORIZED) -> None:
        self.access = Access.NOT_DETERMINED
        self.answer = answer
        self.prompts = 0
        self.reads = 0
        self.settings_opened = 0
        self.release = threading.Event()
        self.release.set()

    def request(self) -> Access:
        self.prompts += 1
        self.access = self.answer
        return self.access

    def items(self, _data_dir: Path, _user: str, limit: int | None):
        self.reads += 1
        self.release.wait(5)
        found = [
            make_item(UUID_A, preview_path=preview_ref(UUID_A)),
            make_item(UUID_B, preview_path=preview_ref(UUID_B)),
            make_item(UUID_C, preview_path=preview_ref(UUID_C), albums=("Private",)),
        ]
        return found[:limit] if limit else found

    def open_settings(self) -> None:
        self.settings_opened += 1

    def port(self) -> PhotosPort:
        return PhotosPort(
            status=lambda: self.access,
            request=self.request,
            items=self.items,
            preview=lambda uuid, edge: None,
            open_settings=self.open_settings,
        )


@pytest.fixture(autouse=True)
def _previews_off() -> Iterator[None]:
    yield
    previews.disable_photokit()


@pytest.fixture
def photos() -> FakePhotos:
    return FakePhotos()


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def onboarding(tmp_path: Path, session: FakeSession, photos: FakePhotos) -> Onboarding:
    return Onboarding(
        session,  # type: ignore[arg-type] - duck-typed Session
        photos.port(),
        tmp_path,
        USER,
        Exclusions(albums=frozenset({"Private"})),
        clock=lambda: NOW,
    )


def test_photos_is_not_touched_before_sign_in(onboarding: Onboarding, photos: FakePhotos) -> None:
    with pytest.raises(SetupError) as raised:
        onboarding.connect_photos()
    assert raised.value.status == 401
    with pytest.raises(SetupError):
        onboarding.start_scan()
    assert (photos.prompts, photos.reads) == (0, 0)
    assert load_preview(preview_ref(UUID_A)) is SkipReason.NO_ACCESS


def test_wrong_password_is_reported(onboarding: Onboarding) -> None:
    with pytest.raises(SetupError, match="Wrong email or password"):
        onboarding.sign_in(EMAIL, "nope")
    assert onboarding.account() is None


def test_connect_records_the_granting_user(
    onboarding: Onboarding, photos: FakePhotos, tmp_path: Path
) -> None:
    onboarding.sign_in(EMAIL, PASSWORD)
    assert onboarding.connect_photos() is Access.AUTHORIZED
    assert photos.prompts == 1
    assert granted_user(tmp_path) == USER
    assert load_preview(preview_ref(UUID_A)) is SkipReason.MISSING_FILE
    onboarding.connect_photos()
    assert photos.prompts == 1


def test_denied_access_records_nothing(tmp_path: Path, session: FakeSession) -> None:
    denied = FakePhotos(answer=Access.DENIED)
    onboarding = Onboarding(session, denied.port(), tmp_path, USER, Exclusions())  # type: ignore[arg-type]
    onboarding.sign_in(EMAIL, PASSWORD)
    with pytest.raises(SetupError) as raised:
        onboarding.connect_photos()
    assert raised.value.status == 403
    assert granted_user(tmp_path) is None
    onboarding.open_photos_settings()
    assert denied.settings_opened == 1


def test_grant_from_another_user_is_refused(
    onboarding: Onboarding, photos: FakePhotos, tmp_path: Path
) -> None:
    record_grant(tmp_path, "someone-else", NOW)
    photos.access = Access.AUTHORIZED
    onboarding.sign_in(EMAIL, PASSWORD)
    with pytest.raises(SetupError, match="someone-else"):
        onboarding.connect_photos()
    with pytest.raises(SetupError):
        onboarding.start_scan()
    assert photos.reads == 0


def test_scan_runs_in_background_and_applies_exclusions(
    onboarding: Onboarding, photos: FakePhotos
) -> None:
    onboarding.sign_in(EMAIL, PASSWORD)
    onboarding.connect_photos()
    photos.release.clear()
    thread = onboarding.start_scan()
    with pytest.raises(SetupError) as raised:
        onboarding.start_scan()
    assert raised.value.status == 409
    assert onboarding.summary()["ready"] is False
    photos.release.set()
    thread.join(5)
    progress = onboarding.progress()
    assert progress.state is ScanState.DONE
    assert (progress.seen, progress.scanned, progress.excluded) == (3, 3, 1)
    summary = onboarding.summary()
    assert summary["ready"] is True
    assert summary["library"]["items"] == 3  # type: ignore[index]


def test_sign_out_turns_previews_off(onboarding: Onboarding) -> None:
    onboarding.sign_in(EMAIL, PASSWORD)
    onboarding.connect_photos()
    onboarding.sign_out()
    assert load_preview(preview_ref(UUID_A)) is SkipReason.NO_ACCESS
    assert onboarding.photos_ready() is False


def test_server_down_is_a_setup_error(onboarding: Onboarding, session: FakeSession) -> None:
    session.server_down = True
    with pytest.raises(SetupError) as raised:
        onboarding.summary()
    assert raised.value.status == 502


def test_claude_needs_a_session(session: FakeSession) -> None:
    build = proxy_client_factory("https://loupe.example.test", session)  # type: ignore[arg-type]
    with pytest.raises(StudioError, match="Sign in"):
        build()


# Over HTTP ---------------------------------------------------------------------


def _no_client():
    raise AssertionError("Claude is not called in these tests")


def _call(port: int, method: str, path: str, token: str, body: object = None) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": f"127.0.0.1:{port}"}
    payload = None
    if method == "POST":
        headers |= {"Content-Type": "application/json", TOKEN_HEADER: token}
        payload = json.dumps(body or {}).encode()
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    data = json.loads(response.read() or b"{}")
    connection.close()
    return response.status, data


def test_setup_flow_over_http(tmp_path: Path, onboarding: Onboarding) -> None:
    settings = Settings(
        data_dir=tmp_path, photos_library=None, album_folder="Loupe", owner_macos_user=None
    )
    studio = Studio(settings, make_brand(tmp_path), _no_client, None, None)  # type: ignore[arg-type]
    app = App(studio, onboarding=onboarding)
    port = _free_port()
    server = make_server(app, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, setup = _call(port, "GET", "/api/setup", app.token)
        assert status == 200
        assert (setup["mode"], setup["account"], setup["ready"]) == ("app", None, False)
        assert _call(port, "GET", "/api/state", app.token)[0] == 403
        assert _call(port, "POST", "/api/setup/photos", app.token)[0] == 401
        assert _call(port, "POST", "/api/setup/sign-in", app.token, {"email": EMAIL})[0] == 400
        status, setup = _call(
            port, "POST", "/api/setup/sign-in", app.token, {"email": EMAIL, "password": PASSWORD}
        )
        assert (status, setup["account"]["email"]) == (200, EMAIL)
        status, setup = _call(port, "POST", "/api/setup/photos", app.token)
        assert (status, setup["photos"]["ready"]) == (200, True)
        assert _call(port, "POST", "/api/setup/scan", app.token, {"limit": 0})[0] == 400
        assert _call(port, "POST", "/api/setup/scan", app.token)[0] == 200
        for _ in range(50):
            status, setup = _call(port, "GET", "/api/setup", app.token)
            if setup["ready"]:
                break
            threading.Event().wait(0.05)
        assert setup["ready"] is True
        assert _call(port, "GET", "/api/state", app.token)[0] == 200
    finally:
        server.shutdown()
        server.server_close()
