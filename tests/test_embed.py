from collections.abc import Sequence
from pathlib import Path

import numpy as np
from PIL import Image

from loupe.config import Exclusions
from loupe.embed import SkipReason, embed_items, vector_path
from loupe.index import connect
from loupe.scan import scan_items
from loupe.vectors import MIN_CAPACITY, VectorStore
from tests.fixtures import UUID_A, UUID_B, UUID_C, make_item

DIM = 4
NO_EXCLUSIONS = Exclusions()


class FakeEncoder:
    """Maps each image's mean red value to a fixed unit vector."""

    model_id = "fake/test"
    dim = DIM

    def preprocess(self, image: Image.Image) -> np.ndarray:
        return np.asarray(image, dtype=np.float32).mean(axis=(0, 1))

    def encode(self, batch: Sequence[np.ndarray]) -> np.ndarray:
        rows = np.zeros((len(batch), DIM), dtype=np.float32)
        rows[:, 0] = 1.0
        return rows


def _image(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    Image.new("RGB", (8, 8), (200, 10, 10)).save(path)
    return str(path)


def _setup(tmp_path: Path, items: list) -> tuple:
    connection = connect(tmp_path / "index.sqlite")
    scan_items(connection, items, NO_EXCLUSIONS)
    store = VectorStore(vector_path(tmp_path, FakeEncoder.model_id), DIM)
    return connection, store


def test_embeds_previews_and_records_skips(tmp_path: Path) -> None:
    broken = tmp_path / "broken.jpeg"
    broken.write_bytes(b"not an image")
    connection, store = _setup(
        tmp_path,
        [
            make_item(UUID_A, preview_path=_image(tmp_path, "a.jpeg")),
            make_item(UUID_B, preview_path=str(broken)),
            make_item(UUID_C, preview_path=None),
        ],
    )
    result = embed_items(connection, store, FakeEncoder(), batch_size=2)
    assert result.embedded == 1
    assert result.skipped == {SkipReason.UNREADABLE: 1, SkipReason.NO_PREVIEW: 1}
    row = connection.execute("SELECT vector_row FROM embeddings WHERE uuid = ?", (UUID_A,))
    assert store.read([row.fetchone()[0]])[0, 0] == 1.0
    skips = dict(connection.execute("SELECT uuid, reason FROM embed_skips"))
    assert skips == {UUID_B: SkipReason.UNREADABLE, UUID_C: SkipReason.NO_PREVIEW}


def test_rerun_resumes_and_limit_applies(tmp_path: Path) -> None:
    connection, store = _setup(
        tmp_path,
        [
            make_item(uuid, preview_path=_image(tmp_path, f"{uuid}.png"))
            for uuid in (UUID_A, UUID_B)
        ],
    )
    first = embed_items(connection, store, FakeEncoder(), limit=1)
    second = embed_items(connection, store, FakeEncoder())
    third = embed_items(connection, store, FakeEncoder())
    assert (first.embedded, second.embedded, third.embedded) == (1, 1, 0)
    rows = [row[0] for row in connection.execute("SELECT vector_row FROM embeddings ORDER BY 1")]
    assert rows == [0, 1]


def test_items_excluded_later_are_purged(tmp_path: Path) -> None:
    preview = _image(tmp_path, "a.jpeg")
    connection, store = _setup(tmp_path, [make_item(UUID_A, preview_path=preview)])
    embed_items(connection, store, FakeEncoder())
    scan_items(
        connection,
        [make_item(UUID_A, preview_path=preview, albums=("Private",))],
        Exclusions(albums=frozenset({"Private"})),
    )
    result = embed_items(connection, store, FakeEncoder())
    assert result.purged == 1
    assert connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
    assert not store.read([0]).any()


def test_vector_store_grows_and_reopens(tmp_path: Path) -> None:
    path = tmp_path / "vectors.npy"
    store = VectorStore(path, DIM)
    store.write(MIN_CAPACITY, np.ones((2, DIM), dtype=np.float32))
    assert store.capacity >= MIN_CAPACITY + 2
    reopened = VectorStore(path, DIM)
    assert reopened.read([MIN_CAPACITY + 1]).tolist() == [[1.0] * DIM]
