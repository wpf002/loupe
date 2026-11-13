"""Server side of Sync: checks each upload from Loupe.app, then writes it to the account's data.

Every function here takes the account's paths and the raw upload, so the HTTP layer only
routes and sizes requests. Previews and vectors are accepted for eligible items only.
"""

import io
import json
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from loupe.brand import Colors, Voice
from loupe.config import PillarConfig
from loupe.embed import vector_path
from loupe.encoder import MODEL_ID
from loupe.index import connect, is_eligible
from loupe.train import TrainingError, load_model, model_path
from loupe.vectors import VectorStore, VectorStoreError

UUID_PATTERN = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
MAX_PREVIEW_EDGE = 2048
MAX_PAGE = 1000
MAX_VECTOR_BATCH = 4096
FONT_ROLES = frozenset({"display", "body"})
FONT_SUFFIXES = frozenset({"ttf", "otf", "ttc"})
# First four bytes of TrueType, OpenType CFF and TrueType collection files.
FONT_MAGIC = (b"\x00\x01\x00\x00", b"true", b"OTTO", b"ttcf")
BRAND_FILE = "brand.json"
PILLARS_FILE = "pillars.json"
FONTS_DIR = "fonts"


class SyncStoreError(Exception):
    """The upload was refused. The message is safe to show."""


class BrandUpload(BaseModel):
    """brand.toml without font paths. Fonts arrive as their own uploads."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=80)
    colors: Colors
    voice: Voice


class ConfigUpload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    brand: BrandUpload | None
    pillars: PillarConfig | None


def _write_atomic(target: Path, body: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f".{target.name}.part")
    staging.write_bytes(body)
    os.replace(staging, target)


def check_uuid(value: str) -> str:
    if not UUID_PATTERN.match(value):
        raise SyncStoreError("Not a photo ID")
    return value.upper()


def _eligible(index_path: Path, uuid: str) -> bool:
    connection = connect(index_path)
    try:
        return is_eligible(connection, uuid)
    finally:
        connection.close()


def store_preview(
    index_path: Path, previews_dir: Path, thumbs_dir: Path, uuid: str, body: bytes
) -> None:
    uuid = check_uuid(uuid)
    if not _eligible(index_path, uuid):
        raise SyncStoreError("That photo isn't in the synced library")
    try:
        with Image.open(io.BytesIO(body)) as image:
            image.verify()
        with Image.open(io.BytesIO(body)) as image:
            if image.format != "JPEG":
                raise SyncStoreError("Previews must be JPEG")
            if max(image.size) > MAX_PREVIEW_EDGE:
                raise SyncStoreError("Preview is too large")
    except (UnidentifiedImageError, OSError, SyntaxError) as error:
        raise SyncStoreError("Preview is not a readable image") from error
    _write_atomic(previews_dir / f"{uuid}.jpg", body)
    (thumbs_dir / f"{uuid}.jpg").unlink(missing_ok=True)


def _eligible_after(connection: sqlite3.Connection, after: str) -> list[str]:
    rows = connection.execute(
        "SELECT uuid FROM items WHERE excluded_reason IS NULL AND uuid > ? ORDER BY uuid",
        (after,),
    )
    return [row[0] for row in rows]


def missing_previews(index_path: Path, previews_dir: Path, after: str, limit: int) -> list[str]:
    """Eligible UUIDs after `after`, in order, whose preview is not on the server yet."""
    limit = max(1, min(limit, MAX_PAGE))
    connection = connect(index_path)
    try:
        candidates = _eligible_after(connection, after)
    finally:
        connection.close()
    missing: list[str] = []
    for uuid in candidates:
        if not (previews_dir / f"{uuid}.jpg").is_file():
            missing.append(uuid)
            if len(missing) == limit:
                break
    return missing


def missing_vectors(index_path: Path, after: str, limit: int) -> list[str]:
    limit = max(1, min(limit, MAX_PAGE))
    connection = connect(index_path)
    try:
        rows = connection.execute(
            "SELECT uuid FROM items WHERE excluded_reason IS NULL AND uuid > ? "
            "AND uuid NOT IN (SELECT uuid FROM embeddings WHERE model = ?) "
            "ORDER BY uuid LIMIT ?",
            (after, MODEL_ID, limit),
        )
        return [row[0] for row in rows]
    finally:
        connection.close()


def _read_vectors(body: bytes) -> tuple[list[str], np.ndarray]:
    try:
        with np.load(io.BytesIO(body), allow_pickle=False) as archive:
            model = str(archive["model"])
            uuids = [check_uuid(str(value)) for value in archive["uuids"]]
            vectors = np.asarray(archive["vectors"])
    except (KeyError, ValueError, OSError) as error:
        raise SyncStoreError("Vector upload could not be read") from error
    if model != MODEL_ID:
        raise SyncStoreError("Update Loupe.app, then sync again")
    if vectors.ndim != 2 or vectors.dtype != np.float16 or len(vectors) != len(uuids):
        raise SyncStoreError("Vector upload has the wrong shape")
    if len(uuids) > MAX_VECTOR_BATCH or len(set(uuids)) != len(uuids):
        raise SyncStoreError("Vector upload has too many or repeated IDs")
    if not np.isfinite(vectors).all():
        raise SyncStoreError("Vector upload has invalid numbers")
    return uuids, vectors


def store_vectors(index_path: Path, data_dir: Path, body: bytes) -> int:
    """Add vectors for eligible items that have none yet. Returns how many were added."""
    uuids, vectors = _read_vectors(body)
    connection = connect(index_path)
    try:
        wanted = set(missing_vectors_among(connection, uuids))
        keep = [offset for offset, uuid in enumerate(uuids) if uuid in wanted]
        if not keep:
            return 0
        try:
            store = VectorStore(vector_path(data_dir, MODEL_ID), vectors.shape[1])
        except VectorStoreError as error:
            raise SyncStoreError("Vector width doesn't match this library") from error
        first_row = int(
            connection.execute(
                "SELECT COALESCE(MAX(vector_row) + 1, 0) FROM embeddings WHERE model = ?",
                (MODEL_ID,),
            ).fetchone()[0]
        )
        # Vectors reach disk before their rows commit, as in `loupe embed`.
        store.write(first_row, vectors[keep])
        now = datetime.now(UTC).isoformat()
        with connection:
            connection.executemany(
                "INSERT INTO embeddings (model, uuid, vector_row, embedded_at) VALUES (?, ?, ?, ?)",
                [
                    (MODEL_ID, uuids[offset], first_row + row, now)
                    for row, offset in enumerate(keep)
                ],
            )
        return len(keep)
    finally:
        connection.close()


def missing_vectors_among(connection: sqlite3.Connection, uuids: list[str]) -> list[str]:
    """The subset of `uuids` that is eligible and has no vector yet."""
    connection.execute("CREATE TEMP TABLE IF NOT EXISTS offered (uuid TEXT PRIMARY KEY)")
    connection.execute("DELETE FROM temp.offered")
    connection.executemany("INSERT OR IGNORE INTO temp.offered VALUES (?)", [(u,) for u in uuids])
    rows = connection.execute(
        "SELECT o.uuid FROM temp.offered o JOIN items i ON i.uuid = o.uuid "
        "WHERE i.excluded_reason IS NULL "
        "AND o.uuid NOT IN (SELECT uuid FROM embeddings WHERE model = ?)",
        (MODEL_ID,),
    ).fetchall()
    connection.execute("DELETE FROM temp.offered")
    connection.commit()
    return [row[0] for row in rows]


def store_model(data_dir: Path, body: bytes) -> None:
    target = model_path(data_dir)
    staging = target.with_name("upload.tmp.npz")
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(body)
    try:
        model = load_model(staging)
    except (TrainingError, KeyError, ValueError, OSError) as error:
        staging.unlink(missing_ok=True)
        raise SyncStoreError("Pillar model could not be read") from error
    if model.model_id != MODEL_ID or model.coef.ndim != 2 or len(model.pillars) != len(model.coef):
        staging.unlink(missing_ok=True)
        raise SyncStoreError("Update Loupe.app, then sync again")
    os.replace(staging, target)


def store_config(config_dir: Path, body: bytes) -> None:
    try:
        upload = ConfigUpload.model_validate_json(body)
    except ValidationError as error:
        raise SyncStoreError("Brand or pillar settings are invalid") from error
    if upload.pillars is not None and upload.pillars.exclusions.model_dump(exclude_defaults=True):
        raise SyncStoreError("Exclusions stay on the Mac")
    for name, part in ((BRAND_FILE, upload.brand), (PILLARS_FILE, upload.pillars)):
        target = config_dir / name
        if part is None:
            target.unlink(missing_ok=True)
        else:
            _write_atomic(target, json.dumps(part.model_dump(mode="json")).encode())


def store_font(config_dir: Path, role: str, suffix: str, body: bytes) -> None:
    if role not in FONT_ROLES or suffix not in FONT_SUFFIXES:
        raise SyncStoreError("Fonts are display or body, as .ttf, .otf or .ttc")
    if body[:4] not in FONT_MAGIC:
        raise SyncStoreError("That file isn't a font")
    fonts = config_dir / FONTS_DIR
    for other in FONT_SUFFIXES - {suffix}:
        (fonts / f"{role}.{other}").unlink(missing_ok=True)
    _write_atomic(fonts / f"{role}.{suffix}", body)
