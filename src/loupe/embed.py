"""M2: embed every eligible preview. Keyed by UUID, so a re-run picks up where it stopped."""

import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import batched
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from PIL import Image

from loupe.index import eligible_uuids
from loupe.previews import SkipReason, load_preview
from loupe.vectors import VectorStore

BATCH_SIZE = 32
LOADER_THREADS = 8


class ImageEncoder(Protocol):
    model_id: str
    dim: int

    def preprocess(self, image: Image.Image) -> Any: ...

    def encode(self, batch: Sequence[Any]) -> np.ndarray:
        """Return one L2-normalized row per input."""
        ...


@dataclass
class EmbedResult:
    embedded: int = 0
    purged: int = 0
    seconds: float = 0.0
    skipped: Counter[str] = field(default_factory=Counter)

    @property
    def per_second(self) -> float:
        return self.embedded / self.seconds if self.seconds else 0.0


@dataclass(frozen=True)
class Prepared:
    uuid: str
    tensor: Any | None
    skip: SkipReason | None


def vector_path(data_dir: Path, model_id: str) -> Path:
    slug = "".join(char if char.isalnum() else "-" for char in model_id)
    return data_dir / "embeddings" / f"{slug}.f16.npy"


def pending_work(
    connection: sqlite3.Connection, model_id: str, limit: int | None
) -> list[tuple[str, str | None]]:
    """Eligible UUIDs with no vector for this model yet, with their preview paths."""
    done = {
        row[0]
        for row in connection.execute("SELECT uuid FROM embeddings WHERE model = ?", (model_id,))
    }
    previews = dict(
        connection.execute("SELECT uuid, preview_path FROM items WHERE excluded_reason IS NULL")
    )
    pending = [uuid for uuid in eligible_uuids(connection) if uuid not in done]
    if limit is not None:
        pending = pending[:limit]
    return [(uuid, previews.get(uuid)) for uuid in pending]


def purge_ineligible(connection: sqlite3.Connection, store: VectorStore, model_id: str) -> int:
    """Zero and forget vectors for items that were excluded after they were embedded."""
    rows = connection.execute(
        """
        SELECT embeddings.uuid, embeddings.vector_row FROM embeddings
        JOIN items ON items.uuid = embeddings.uuid
        WHERE embeddings.model = ? AND items.excluded_reason IS NOT NULL
        """,
        (model_id,),
    ).fetchall()
    if not rows:
        return 0
    store.clear([vector_row for _, vector_row in rows])
    with connection:
        connection.executemany(
            "DELETE FROM embeddings WHERE model = ? AND uuid = ?",
            [(model_id, uuid) for uuid, _ in rows],
        )
    return len(rows)


def _prepare(
    encoder: ImageEncoder, work: tuple[str, str | None], loader: Callable[[str | None], Any]
) -> Prepared:
    uuid, preview_path = work
    loaded = loader(preview_path)
    if isinstance(loaded, SkipReason):
        return Prepared(uuid, None, loaded)
    return Prepared(uuid, encoder.preprocess(loaded), None)


def _next_row(connection: sqlite3.Connection, model_id: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(vector_row) + 1, 0) FROM embeddings WHERE model = ?", (model_id,)
    ).fetchone()
    return int(row[0])


def _record_batch(
    connection: sqlite3.Connection,
    store: VectorStore,
    encoder: ImageEncoder,
    prepared: list[Prepared],
) -> int:
    ready = [item for item in prepared if item.tensor is not None]
    skipped = [item for item in prepared if item.skip is not None]
    now = datetime.now(UTC).isoformat()
    first_row = _next_row(connection, encoder.model_id)
    if ready:
        # Vectors hit disk before their rows are committed; a crash in between only
        # leaves unclaimed rows that the next run overwrites.
        store.write(first_row, encoder.encode([item.tensor for item in ready]))
    with connection:
        connection.executemany(
            "INSERT INTO embeddings (model, uuid, vector_row, embedded_at) VALUES (?, ?, ?, ?)",
            [
                (encoder.model_id, item.uuid, first_row + offset, now)
                for offset, item in enumerate(ready)
            ],
        )
        connection.executemany(
            "DELETE FROM embed_skips WHERE model = ? AND uuid = ?",
            [(encoder.model_id, item.uuid) for item in ready],
        )
        connection.executemany(
            "INSERT OR REPLACE INTO embed_skips (model, uuid, reason) VALUES (?, ?, ?)",
            [(encoder.model_id, item.uuid, str(item.skip)) for item in skipped],
        )
    return len(ready)


def embed_items(
    connection: sqlite3.Connection,
    store: VectorStore,
    encoder: ImageEncoder,
    limit: int | None = None,
    batch_size: int = BATCH_SIZE,
    loader: Callable[[str | None], Any] = load_preview,
    on_batch: Callable[[int], None] | None = None,
) -> EmbedResult:
    result = EmbedResult(purged=purge_ineligible(connection, store, encoder.model_id))
    work = pending_work(connection, encoder.model_id, limit)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=LOADER_THREADS) as pool:
        for batch in _batches(work, batch_size):
            prepared = list(pool.map(lambda item: _prepare(encoder, item, loader), batch))
            result.embedded += _record_batch(connection, store, encoder, prepared)
            result.skipped.update(str(item.skip) for item in prepared if item.skip)
            if on_batch is not None:
                on_batch(len(batch))
    result.seconds = time.monotonic() - started
    return result


def _batches(
    work: list[tuple[str, str | None]], batch_size: int
) -> Iterator[tuple[tuple[str, str | None], ...]]:
    return batched(work, batch_size)
