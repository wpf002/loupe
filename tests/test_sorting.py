"""Library and Search jobs for Loupe.app, run against synthetic previews and fake encoders."""

import threading
import time
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from loupe.config import Exclusions, PillarConfig
from loupe.index import connect
from loupe.scan import scan_items
from loupe.search import Facet
from loupe.sorting import (
    ALBUMS_FAILED,
    ENCODER_BUSY,
    JOB_RUNNING,
    NOT_SORTED,
    RUNNING,
    Sorter,
    SortError,
    Stage,
    too_few_examples,
)
from tests.fixtures import make_item, synthetic_uuid

DIM = 4
JOB_TIMEOUT_SECONDS = 30
CONFIG = PillarConfig(
    label_album_prefixes=("CC ",),
    # Regularization keeps probabilities near 0.75 on this small synthetic set.
    min_confidence=0.7,
    review_floor=0.4,
    min_examples_per_pillar=10,
)
# Each group's previews share a colour; the fake encoder turns colour into a vector.
RED, GREEN, BLUE = (220, 20, 20), (20, 220, 20), (20, 20, 220)
UNLABELED_RED = 6
TEXTS = {"red": [1.0, 0.0, 0.0, 0.0]}


class FakeEncoder:
    model_id = "fake/sorting"
    dim = DIM

    def preprocess(self, image: Image.Image) -> np.ndarray:
        return np.asarray(image, dtype=np.float32).mean(axis=(0, 1)) / 255

    def encode(self, batch: Sequence[np.ndarray]) -> np.ndarray:
        rows = np.array([[*rgb, 0.1] for rgb in batch], dtype=np.float32)
        return rows / np.linalg.norm(rows, axis=1, keepdims=True)

    def encode_texts(self, texts: Sequence[str]) -> np.ndarray:
        return np.array([TEXTS.get(text, [0, 0, 0, 1]) for text in texts], dtype=np.float32)


class BlockingEncoder(FakeEncoder):
    """Holds the first batch until released, so a test can act while embedding runs."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def encode(self, batch: Sequence[np.ndarray]) -> np.ndarray:
        self.started.set()
        self.release.wait(JOB_TIMEOUT_SECONDS)
        return super().encode(batch)


class RecordingWriter:
    def __init__(self) -> None:
        self.albums: dict[str, list[str]] = {}

    def ensure_album(self, album: str) -> None:
        self.albums.setdefault(album, [])

    def add(self, album: str, uuids: list[str]) -> int:
        self.albums[album].extend(uuids)
        return len(uuids)


class BrokenWriter(RecordingWriter):
    def add(self, album: str, uuids: list[str]) -> int:
        raise RuntimeError("synthetic failure")


def _library(tmp_path: Path, per_album: int = 15) -> Path:
    """Travel is red, food is green. Unlabeled: 6 red (travel picks), 6 blue (no pillar)."""
    rng = np.random.default_rng(0)
    items, number = [], 0
    plan = [
        ("CC Travel", RED, per_album),
        ("CC Food", GREEN, per_album),
        (None, RED, UNLABELED_RED),
        (None, BLUE, 6),
    ]
    for album, base, count in plan:
        for _ in range(count):
            number += 1
            jitter = rng.integers(-15, 15, 3)
            colour = tuple(int(np.clip(c + j, 0, 255)) for c, j in zip(base, jitter, strict=True))
            preview = tmp_path / f"preview-{number}.png"
            Image.new("RGB", (8, 8), colour).save(preview)
            items.append(
                make_item(
                    synthetic_uuid(number),
                    albums=(album,) if album else (),
                    persons=(),
                    preview_path=str(preview),
                )
            )
    index_path = tmp_path / "index.sqlite"
    connection = connect(index_path)
    scan_items(connection, items, Exclusions())
    connection.close()
    return index_path


def _sorter(tmp_path: Path, encoder: FakeEncoder | None = None, writer=None) -> Sorter:
    index_path = _library(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    chosen = encoder or FakeEncoder()
    recording = writer or RecordingWriter()
    return Sorter(data_dir, index_path, CONFIG, lambda: chosen, lambda: recording)


def _wait(sorter: Sorter) -> Stage:
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    while sorter.progress().stage in RUNNING:
        assert time.monotonic() < deadline, "job did not finish"
        time.sleep(0.02)
    return sorter.progress().stage


def test_status_before_sorting_lists_pillars_without_scores(tmp_path: Path) -> None:
    status = _sorter(tmp_path).status()
    assert status["stage"] == "idle"
    assert (status["items"], status["embedded"], status["sorted"]) == (42, 0, False)
    rows = {row["pillar"]: row for row in status["pillars"]}
    assert {name: row["examples"] for name, row in rows.items()} == {"CC Food": 15, "CC Travel": 15}
    assert not any(row["trained"] for row in rows.values())


def test_sort_embeds_trains_and_scores(tmp_path: Path) -> None:
    sorter = _sorter(tmp_path)
    sorter.start_sort()
    assert _wait(sorter) == Stage.DONE
    status = sorter.status()
    assert (status["embedded"], status["sorted"]) == (42, True)
    for row in status["pillars"]:
        assert row["trained"]
        assert row["precision"] is not None
        assert row["in_album"] == 0
        assert row["to_add"] == row["confident"]


def test_album_job_adds_confident_picks_only_to_loupe_albums(tmp_path: Path) -> None:
    writer = RecordingWriter()
    sorter = _sorter(tmp_path, writer=writer)
    sorter.start_sort()
    _wait(sorter)
    expected = {row["album"]: row["to_add"] for row in sorter.status()["pillars"]}
    assert expected["Travel"] == UNLABELED_RED
    sorter.start_albums()
    assert _wait(sorter) == Stage.DONE
    assert {album: len(uuids) for album, uuids in writer.albums.items()} == {"Travel": 6}
    status = sorter.status()
    assert all(row["to_add"] == 0 for row in status["pillars"])
    assert status["done"] == sum(expected.values())


def test_albums_need_a_sort_first(tmp_path: Path) -> None:
    with pytest.raises(SortError, match=NOT_SORTED):
        _sorter(tmp_path).start_albums()


def test_failed_album_write_reports_a_short_message(tmp_path: Path) -> None:
    sorter = _sorter(tmp_path, writer=BrokenWriter())
    sorter.start_sort()
    _wait(sorter)
    sorter.start_albums()
    assert _wait(sorter) == Stage.FAILED
    assert sorter.progress().error == ALBUMS_FAILED


def test_too_few_examples_explains_the_minimum(tmp_path: Path) -> None:
    index_path = _library(tmp_path, per_album=3)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sorter = Sorter(data_dir, index_path, CONFIG, FakeEncoder, RecordingWriter)
    sorter.start_sort()
    assert _wait(sorter) == Stage.FAILED
    assert sorter.progress().error == too_few_examples(CONFIG)
    assert sorter.status()["sorted"] is False


def test_one_job_at_a_time_and_search_waits_for_embedding(tmp_path: Path) -> None:
    encoder = BlockingEncoder()
    sorter = _sorter(tmp_path, encoder=encoder)
    sorter.start_sort()
    assert encoder.started.wait(JOB_TIMEOUT_SECONDS)
    with pytest.raises(SortError, match=JOB_RUNNING):
        sorter.start_sort()
    with pytest.raises(SortError, match=ENCODER_BUSY):
        sorter.search("red", None, None, {})
    assert sorter.progress().total == 42
    encoder.release.set()
    assert _wait(sorter) == Stage.DONE


def test_search_ranks_by_text_and_filters_by_pillar(tmp_path: Path) -> None:
    sorter = _sorter(tmp_path)
    with pytest.raises(SortError, match=NOT_SORTED):
        sorter.search("red", None, None, {})
    sorter.start_sort()
    _wait(sorter)
    hits = sorter.search("red", None, None, {})
    travel = {synthetic_uuid(n) for n in range(1, 16)}
    red = travel | {synthetic_uuid(n) for n in range(31, 31 + UNLABELED_RED)}
    assert {hit.uuid for hit in hits[: len(red)]} == red
    in_food = sorter.search("red", "CC Food", None, {})
    assert in_food and not {hit.uuid for hit in in_food} & travel


def test_search_rejects_bad_filters(tmp_path: Path) -> None:
    sorter = _sorter(tmp_path)
    sorter.start_sort()
    _wait(sorter)
    with pytest.raises(SortError, match="No such pillar"):
        sorter.search("red", "CC Nowhere", None, {})
    with pytest.raises(SortError, match="photos or videos"):
        sorter.search("red", None, "audio", {})
    with pytest.raises(SortError):
        sorter.search("red", None, None, {Facet.LIGHT: "neon"})
