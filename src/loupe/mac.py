"""M15 Loupe.app backend: the local server the native window loads.

The Swift launcher in `macapp/` starts this as a child process, so PhotoKit sees
Loupe.app (its Info.plist and its privacy grant). It prints one line with the URL,
then serves until the launcher closes stdin. Data and config live in the user's
Application Support folder, never in the repo.
"""

import logging
import socket
import sqlite3
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TextIO

from loupe.brand import BrandError, BrandProfile, load_brand
from loupe.claude import StructuredClient
from loupe.config import ConfigError, Exclusions, PillarConfig, load_pillar_config
from loupe.settings import DEFAULT_ALBUM_FOLDER, Settings
from loupe.studio import NO_BRAND, Studio, StudioError, WeekPlanner, search_planner
from loupe.week import PlannedWeek

APP_SUPPORT = Path("~/Library/Application Support/Loupe")
BRAND_FILENAME = "brand.toml"
URL_PREFIX = "LOUPE_URL "
HOST = "127.0.0.1"
NO_PILLARS = "No pillars yet. Add pillars.toml to the Loupe config folder."
NO_ORIGINALS = "Reels need the original video, which Loupe.app can't open yet."
# A new user has no pillars.toml. Her "CC " albums are the pillars, with the repo defaults.
DEFAULT_PILLARS = PillarConfig(
    label_album_prefixes=("CC ",),
    min_confidence=0.80,
    review_floor=0.50,
    min_examples_per_pillar=30,
)

logger = logging.getLogger("loupe.mac")


class MacAppError(Exception):
    """Startup problem. The launcher shows the message."""


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path
    config_dir: Path


def app_paths(root: Path = APP_SUPPORT) -> AppPaths:
    base = root.expanduser()
    return AppPaths(data_dir=base / "data", config_dir=base / "config")


def load_optional_brand(config_dir: Path) -> BrandProfile | None:
    path = config_dir / BRAND_FILENAME
    if not path.is_file():
        return None
    try:
        return load_brand(path)
    except BrandError as error:
        raise MacAppError(str(error)) from error


def load_optional_pillars(config_dir: Path) -> PillarConfig | None:
    try:
        return load_pillar_config(config_dir)
    except ConfigError as error:
        if not (config_dir / "pillars.toml").is_file():
            return None
        raise MacAppError(str(error)) from error


def unavailable_planner(message: str) -> WeekPlanner:
    def plan(_connection: sqlite3.Connection, _monday: date) -> PlannedWeek:
        raise StudioError(message)

    return plan


def no_originals(_uuid: str) -> Path:
    raise StudioError(NO_ORIGINALS)


def build_studio(
    settings: Settings,
    brand: BrandProfile | None,
    pillars: PillarConfig | None,
    client_factory: Callable[[], StructuredClient],
) -> Studio:
    if brand is None:
        planner = unavailable_planner(NO_BRAND)
    elif pillars is None:
        planner = unavailable_planner(NO_PILLARS)
    else:
        planner = search_planner(settings, pillars, brand, client_factory)
    return Studio(
        settings=settings,
        brand=brand,
        client_factory=client_factory,
        planner=planner,
        find_original=no_originals,
    )


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])


def app_settings(paths: AppPaths, server_url: str) -> Settings:
    return Settings(
        data_dir=paths.data_dir,
        photos_library=None,
        album_folder=DEFAULT_ALBUM_FOLDER,
        owner_macos_user=None,
        server_url=server_url,
    )


def exclusions_for(pillars: PillarConfig | None) -> Exclusions:
    return pillars.exclusions if pillars is not None else Exclusions()


def stop_on_eof(stream: TextIO, stop: Callable[[], None]) -> threading.Thread:
    """The launcher holds our stdin open. When it quits, stdin closes and we stop."""

    def watch() -> None:
        stream.read()
        stop()

    thread = threading.Thread(target=watch, name="loupe-parent-watch", daemon=True)
    thread.start()
    return thread


def run(
    server_url: str,
    port: int | None = None,
    root: Path = APP_SUPPORT,
    web_url: str | None = None,
) -> None:
    """Build everything for the signed-in macOS user, print the URL, serve."""
    from loupe.account import AccountClient, KeychainTokenStore, Session
    from loupe.encoder import SiglipEncoder
    from loupe.onboarding import Onboarding, photokit_port, proxy_client_factory
    from loupe.photokit import PhotoKitWriter
    from loupe.server import App, make_server
    from loupe.settings import current_macos_user
    from loupe.sorting import Sorter
    from loupe.sync import Syncer

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    paths = app_paths(root)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    settings = app_settings(paths, server_url)
    user = current_macos_user()
    pillars = load_optional_pillars(paths.config_dir)
    session = Session(AccountClient(server_url), KeychainTokenStore(user))
    studio = build_studio(
        settings,
        load_optional_brand(paths.config_dir),
        pillars,
        proxy_client_factory(server_url, session),
    )
    onboarding = Onboarding(session, photokit_port(), paths.data_dir, user, exclusions_for(pillars))
    sorter = Sorter(
        paths.data_dir,
        onboarding.index_path,
        pillars or DEFAULT_PILLARS,
        SiglipEncoder,
        lambda: PhotoKitWriter(settings.album_folder, paths.data_dir, user),
    )
    syncer = None
    if web_url is not None:
        syncer = Syncer(
            web_url,
            session.token,
            paths.data_dir,
            onboarding.index_path,
            lambda: load_optional_brand(paths.config_dir),
            pillars or DEFAULT_PILLARS,
        )
    chosen = port or free_port()
    app = App(studio, onboarding=onboarding, sorter=sorter, syncer=syncer)
    server = make_server(app, chosen)
    stop_on_eof(sys.stdin, server.shutdown)
    print(f"{URL_PREFIX}http://{HOST}:{chosen}/", flush=True)  # noqa: T201 - read by the launcher
    logger.info("serving on port %d", chosen)
    try:
        server.serve_forever()
    finally:
        server.server_close()
