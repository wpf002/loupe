"""Pillar config: config/pillars.toml with config/pillars.local.toml merged over it."""

import re
import tomllib
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from loupe.names import validate_album_name

DEFAULT_CONFIG_DIR = Path("config")
PILLARS_FILENAME = "pillars.toml"
LOCAL_PILLARS_FILENAME = "pillars.local.toml"
PHOTOS_UUID_PATTERN = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")


class ConfigError(Exception):
    """Raised when the pillar config is missing or invalid."""


def _validate_uuid(value: str) -> str:
    normalized = value.strip().upper()
    if not PHOTOS_UUID_PATTERN.match(normalized):
        raise ValueError(f"not a Photos UUID: {value!r}")
    return normalized


def _validate_person_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ValueError("person name is empty")
    return name


AlbumName = Annotated[str, AfterValidator(validate_album_name)]
PersonName = Annotated[str, AfterValidator(_validate_person_name)]
PhotosUuid = Annotated[str, AfterValidator(_validate_uuid)]
Probability = Annotated[float, Field(ge=0.0, le=1.0)]


class DateRange(BaseModel):
    """Inclusive on both ends, compared against the item's capture date."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: date
    end: date

    @model_validator(mode="after")
    def _start_not_after_end(self) -> "DateRange":
        if self.start > self.end:
            raise ValueError(f"date range starts after it ends: {self.start} > {self.end}")
        return self

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


class Exclusions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    albums: frozenset[AlbumName] = frozenset()
    persons: frozenset[PersonName] = frozenset()
    date_ranges: tuple[DateRange, ...] = ()
    uuids: frozenset[PhotosUuid] = frozenset()


class PillarConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label_album_prefixes: tuple[str, ...]
    include_albums: frozenset[AlbumName] = frozenset()
    exclude_albums: frozenset[AlbumName] = frozenset()
    min_confidence: Probability
    review_floor: Probability
    min_examples_per_pillar: Annotated[int, Field(ge=1)]
    exclusions: Exclusions = Exclusions()

    @model_validator(mode="after")
    def _floor_below_confidence(self) -> "PillarConfig":
        if self.review_floor >= self.min_confidence:
            raise ValueError("review_floor must be lower than min_confidence")
        return self

    def is_pillar_album(self, album: str) -> bool:
        if album in self.exclude_albums or album in self.exclusions.albums:
            return False
        if album in self.include_albums:
            return True
        return any(album.startswith(prefix) for prefix in self.label_album_prefixes)


def merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Override replaces top-level keys. Tables such as [exclusions] merge key by key."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path} is not valid TOML: {error}") from error


def load_pillar_config(config_dir: Path = DEFAULT_CONFIG_DIR) -> PillarConfig:
    base_path = config_dir / PILLARS_FILENAME
    if not base_path.is_file():
        raise ConfigError(f"missing {base_path}")
    raw = _read_toml(base_path)
    local_path = config_dir / LOCAL_PILLARS_FILENAME
    if local_path.is_file():
        raw = merge_config(raw, _read_toml(local_path))
    try:
        return PillarConfig.model_validate(raw)
    except ValidationError as error:
        raise ConfigError(f"invalid pillar config: {error}") from error
