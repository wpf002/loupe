from pathlib import Path

import numpy as np

from loupe.config import Exclusions, PillarConfig
from loupe.index import connect
from loupe.scan import scan_items
from loupe.train import load_model, load_training_set, model_path, save_model, train_pillars
from loupe.vectors import VectorStore
from tests.fixtures import make_item, seed_vectors, synthetic_uuid

MODEL_ID = "fake/test"
DIM = 8
CONFIG = PillarConfig(
    label_album_prefixes=("CC ",),
    min_confidence=0.8,
    review_floor=0.5,
    min_examples_per_pillar=20,
)


def _cluster(rng: np.random.Generator, axis: int) -> np.ndarray:
    vector = rng.normal(0, 0.15, DIM)
    vector[axis] += 1.0
    return vector / np.linalg.norm(vector)


def build_library(tmp_path: Path) -> tuple:
    """60 travel, 60 food, 5 hotel, 80 unlabeled, 3 screenshots in travel."""
    rng = np.random.default_rng(0)
    items, vectors = [], {}
    plan = [("CC Travel", 0, 60), ("CC Food", 1, 60), ("CC Hotel", 2, 5), (None, 3, 80)]
    number = 0
    for album, axis, count in plan:
        for _ in range(count):
            uuid = synthetic_uuid(number := number + 1)
            items.append(make_item(uuid, albums=(album,) if album else ()))
            vectors[uuid] = _cluster(rng, axis)
    for _ in range(3):
        uuid = synthetic_uuid(number := number + 1)
        items.append(make_item(uuid, albums=("CC Travel",), is_screenshot=True))
        vectors[uuid] = _cluster(rng, 0)
    connection = connect(tmp_path / "index.sqlite")
    scan_items(connection, items, Exclusions())
    store = VectorStore(tmp_path / "vectors.npy", DIM)
    seed_vectors(connection, store, MODEL_ID, vectors)
    return connection, store


def test_trains_pillars_and_reports_small_ones(tmp_path: Path) -> None:
    connection, store = build_library(tmp_path)
    training_set = load_training_set(connection, store, CONFIG, MODEL_ID)
    result = train_pillars(training_set, CONFIG, MODEL_ID)
    assert result.model.pillars == ("CC Food", "CC Travel")
    assert result.too_small == {"CC Hotel": 5}
    assert all(score.precision > 0.9 and score.recall > 0.9 for score in result.scores)
    assert sum(s.train_examples + s.test_examples for s in result.scores) == 120


def test_screenshots_never_train_a_pillar(tmp_path: Path) -> None:
    connection, store = build_library(tmp_path)
    training_set = load_training_set(connection, store, CONFIG, MODEL_ID)
    travel = sum("CC Travel" in labels for labels in training_set.labels)
    assert travel == 60


def test_model_round_trips(tmp_path: Path) -> None:
    connection, store = build_library(tmp_path)
    result = train_pillars(load_training_set(connection, store, CONFIG, MODEL_ID), CONFIG, MODEL_ID)
    path = model_path(tmp_path)
    save_model(result.model, path)
    loaded = load_model(path)
    probe = store.read([0]).astype(np.float32)
    assert np.allclose(loaded.probabilities(probe), result.model.probabilities(probe))
