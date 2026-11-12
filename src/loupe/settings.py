"""Environment settings, validated once at startup."""

import getpass
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError, field_validator

from loupe.names import validate_album_name

DEFAULT_ENV_FILE = Path(".env")
DEFAULT_DATA_DIR = Path("./data")
DEFAULT_ALBUM_FOLDER = "Loupe"
INDEX_FILENAME = "loupe.sqlite"


class SettingsError(Exception):
    """Raised when .env values are missing or invalid."""


class Settings(BaseModel):
    data_dir: Path
    photos_library: Path | None
    album_folder: str
    owner_macos_user: str | None

    @field_validator("album_folder")
    @classmethod
    def _album_folder_is_valid(cls, value: str) -> str:
        return validate_album_name(value)

    @property
    def index_path(self) -> Path:
        return self.data_dir / INDEX_FILENAME


def _blank_to_none(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip()


def load_settings(env_file: Path = DEFAULT_ENV_FILE) -> Settings:
    load_dotenv(env_file)
    library = _blank_to_none(os.environ.get("LOUPE_PHOTOS_LIBRARY"))
    try:
        return Settings(
            data_dir=Path(os.environ.get("LOUPE_DATA_DIR") or DEFAULT_DATA_DIR),
            photos_library=Path(library).expanduser() if library else None,
            album_folder=os.environ.get("LOUPE_ALBUM_FOLDER") or DEFAULT_ALBUM_FOLDER,
            owner_macos_user=_blank_to_none(os.environ.get("LOUPE_OWNER_MACOS_USER")),
        )
    except ValidationError as error:
        raise SettingsError(f"Invalid .env value: {error}") from error


def current_macos_user() -> str:
    return getpass.getuser()


def owner_account_problem(owner_macos_user: str | None, current_user: str) -> str | None:
    """Return why this account may not read the library, or None when it may.

    Loupe refuses to open any Photos library unless .env names the current macOS
    account as the owner's. That keeps a developer's own library out of every run.
    """
    if owner_macos_user is None:
        return "LOUPE_OWNER_MACOS_USER is not set"
    if owner_macos_user != current_user:
        return (
            f"this is macOS account '{current_user}', "
            f"but LOUPE_OWNER_MACOS_USER is '{owner_macos_user}'"
        )
    return None
