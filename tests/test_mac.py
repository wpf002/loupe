"""Loupe.app backend wiring. No PhotoKit, Keychain or network: only the pure parts."""

import io
import threading
from datetime import date
from pathlib import Path

import pytest

from loupe.mac import (
    NO_ORIGINALS,
    NO_PILLARS,
    MacAppError,
    app_paths,
    app_settings,
    build_studio,
    exclusions_for,
    free_port,
    load_optional_brand,
    load_optional_pillars,
    stop_on_eof,
)
from loupe.server import App
from loupe.studio import NO_BRAND, StudioError
from tests.fixtures import UUID_A, make_brand

SERVER_URL = "https://loupe.example.test"


def _no_client():
    raise AssertionError("Claude is not called in these tests")


def test_paths_live_under_application_support(tmp_path: Path) -> None:
    paths = app_paths(tmp_path / "Loupe")
    assert paths.data_dir == tmp_path / "Loupe" / "data"
    assert paths.config_dir == tmp_path / "Loupe" / "config"
    settings = app_settings(paths, SERVER_URL)
    assert (settings.photos_library, settings.owner_macos_user) == (None, None)
    assert settings.server_url == SERVER_URL


def test_new_user_has_no_brand_or_pillars(tmp_path: Path) -> None:
    assert load_optional_brand(tmp_path) is None
    assert load_optional_pillars(tmp_path) is None
    assert exclusions_for(None).albums == frozenset()


def test_broken_config_stops_startup(tmp_path: Path) -> None:
    (tmp_path / "brand.toml").write_text("name = ")
    (tmp_path / "pillars.toml").write_text("[[[")
    with pytest.raises(MacAppError):
        load_optional_brand(tmp_path)
    with pytest.raises(MacAppError):
        load_optional_pillars(tmp_path)


def test_studio_without_brand_explains_each_tab(tmp_path: Path) -> None:
    paths = app_paths(tmp_path)
    studio = build_studio(app_settings(paths, SERVER_URL), None, None, _no_client)
    with pytest.raises(StudioError, match=NO_BRAND):
        studio.profile()
    with pytest.raises(StudioError, match=NO_BRAND):
        studio.planner(None, date(2026, 11, 16))  # type: ignore[arg-type]
    with pytest.raises(StudioError, match=NO_ORIGINALS):
        studio.find_original(UUID_A)
    app = App(studio)
    with pytest.raises(StudioError, match=NO_BRAND):
        app.brand()


def test_brand_without_pillars_cannot_plan(tmp_path: Path) -> None:
    paths = app_paths(tmp_path)
    studio = build_studio(app_settings(paths, SERVER_URL), make_brand(tmp_path), None, _no_client)
    assert studio.profile().name
    with pytest.raises(StudioError, match=NO_PILLARS):
        studio.planner(None, date(2026, 11, 16))  # type: ignore[arg-type]


def test_backend_stops_when_launcher_closes_stdin() -> None:
    stopped = threading.Event()
    thread = stop_on_eof(io.StringIO(""), stopped.set)
    thread.join(5)
    assert stopped.is_set()


def test_free_port_is_usable() -> None:
    assert 1024 <= free_port() <= 65535
