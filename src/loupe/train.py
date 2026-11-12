"""M3: fit one logistic regression per pillar on the embeddings of her labeled albums."""

import hashlib
import os
import random
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression

from loupe.config import PillarConfig
from loupe.vectors import VectorStore

HELD_OUT_PERCENT = 20
BACKGROUND_PER_LABELED = 1.0
BACKGROUND_SEED = 7
REGULARIZATION = 1.0
MAX_ITERATIONS = 2000
MODEL_FILENAME = "pillars.npz"


class TrainingError(Exception):
    """Raised when there is nothing to train on."""


@dataclass(frozen=True)
class TrainingSet:
    """Row i of vectors belongs to uuids[i] with pillar labels labels[i] (empty = background)."""

    uuids: list[str]
    vectors: np.ndarray
    labels: list[frozenset[str]]


@dataclass(frozen=True)
class PillarModel:
    model_id: str
    pillars: tuple[str, ...]
    coef: np.ndarray
    intercept: np.ndarray

    def probabilities(self, vectors: np.ndarray) -> np.ndarray:
        """(items, pillars) probabilities. A row may clear the bar for several pillars."""
        logits = vectors.astype(np.float32) @ self.coef.T + self.intercept
        return 1.0 / (1.0 + np.exp(-logits))


@dataclass(frozen=True)
class PillarScore:
    pillar: str
    train_examples: int
    test_examples: int
    precision: float
    recall: float


@dataclass(frozen=True)
class TrainResult:
    model: PillarModel
    scores: list[PillarScore]
    too_small: dict[str, int]
    background: int


def is_held_out(uuid: str) -> bool:
    """Stable split: the same item lands on the same side on every run."""
    digest = hashlib.sha256(uuid.encode()).digest()
    return int.from_bytes(digest[:2]) % 100 < HELD_OUT_PERCENT


def model_path(data_dir: Path) -> Path:
    return data_dir / "models" / MODEL_FILENAME


def _pillar_albums(connection: sqlite3.Connection, config: PillarConfig) -> dict[str, set[str]]:
    rows = connection.execute(
        """
        SELECT item_albums.uuid, item_albums.album FROM item_albums
        JOIN items ON items.uuid = item_albums.uuid
        WHERE items.excluded_reason IS NULL
        """
    )
    by_uuid: dict[str, set[str]] = {}
    for uuid, album in rows:
        if config.is_pillar_album(album):
            by_uuid.setdefault(uuid, set()).add(album)
    return by_uuid


def embedded_rows(connection: sqlite3.Connection, model_id: str) -> dict[str, int]:
    """Vector rows of eligible items that are allowed into pillars (no screenshots or bursts)."""
    rows = connection.execute(
        """
        SELECT embeddings.uuid, embeddings.vector_row FROM embeddings
        JOIN items ON items.uuid = embeddings.uuid
        WHERE embeddings.model = ? AND items.excluded_reason IS NULL
          AND items.is_screenshot = 0 AND items.is_screen_recording = 0 AND items.is_burst = 0
        ORDER BY embeddings.uuid
        """,
        (model_id,),
    )
    return dict(rows.fetchall())


def load_training_set(
    connection: sqlite3.Connection,
    store: VectorStore,
    config: PillarConfig,
    model_id: str,
    limit: int | None = None,
) -> TrainingSet:
    rows = embedded_rows(connection, model_id)
    albums = _pillar_albums(connection, config)
    labeled = [uuid for uuid in rows if uuid in albums]
    if limit is not None:
        labeled = labeled[:limit]
    unlabeled = [uuid for uuid in rows if uuid not in albums]
    background_size = min(len(unlabeled), round(len(labeled) * BACKGROUND_PER_LABELED))
    # Seeded so the background set is the same on every run; not used for security.
    background = random.Random(BACKGROUND_SEED).sample(unlabeled, background_size)  # noqa: S311
    uuids = labeled + sorted(background)
    vectors = store.read([rows[uuid] for uuid in uuids]).astype(np.float32)
    labels = [frozenset(albums.get(uuid, ())) for uuid in uuids]
    return TrainingSet(uuids, vectors, labels)


def _score(truth: np.ndarray, predicted: np.ndarray) -> tuple[float, float]:
    hits = int(np.sum(truth & predicted))
    precision = hits / int(predicted.sum()) if predicted.any() else 0.0
    recall = hits / int(truth.sum()) if truth.any() else 0.0
    return precision, recall


def _fit_one(vectors: np.ndarray, targets: np.ndarray) -> LogisticRegression:
    classifier = LogisticRegression(
        C=REGULARIZATION, class_weight="balanced", max_iter=MAX_ITERATIONS
    )
    return classifier.fit(vectors, targets)


def train_pillars(training_set: TrainingSet, config: PillarConfig, model_id: str) -> TrainResult:
    counts: dict[str, int] = {}
    for labels in training_set.labels:
        for pillar in labels:
            counts[pillar] = counts.get(pillar, 0) + 1
    kept = sorted(p for p, n in counts.items() if n >= config.min_examples_per_pillar)
    too_small = {p: n for p, n in sorted(counts.items()) if p not in kept}
    if not kept:
        raise TrainingError(
            f"no pillar has {config.min_examples_per_pillar} embedded examples yet."
        )
    held_out = np.array([is_held_out(uuid) for uuid in training_set.uuids])
    scores: list[PillarScore] = []
    coefs: list[np.ndarray] = []
    intercepts: list[float] = []
    for pillar in kept:
        targets = np.array([pillar in labels for labels in training_set.labels])
        train_mask = ~held_out
        if targets[train_mask].all() or not targets[train_mask].any():
            too_small[pillar] = counts[pillar]
            continue
        evaluation = _fit_one(training_set.vectors[train_mask], targets[train_mask])
        probabilities = evaluation.predict_proba(training_set.vectors[held_out])[:, 1]
        precision, recall = _score(targets[held_out], probabilities >= config.min_confidence)
        scores.append(
            PillarScore(
                pillar,
                int(targets[train_mask].sum()),
                int(targets[held_out].sum()),
                precision,
                recall,
            )
        )
        # The model that gets saved sees every example, held-out ones included.
        final = _fit_one(training_set.vectors, targets)
        coefs.append(final.coef_[0])
        intercepts.append(float(final.intercept_[0]))
    model = PillarModel(
        model_id,
        tuple(score.pillar for score in scores),
        np.array(coefs, dtype=np.float32),
        np.array(intercepts, dtype=np.float32),
    )
    background = sum(1 for labels in training_set.labels if not labels)
    return TrainResult(model, scores, too_small, background)


def save_model(model: PillarModel, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(".tmp.npz")
    np.savez(
        staging,
        model_id=np.array(model.model_id),
        pillars=np.array(model.pillars),
        coef=model.coef,
        intercept=model.intercept,
    )
    os.replace(staging, path)


def load_model(path: Path) -> PillarModel:
    if not path.is_file():
        raise TrainingError(f"{path.name} does not exist. Run `loupe train` first.")
    with np.load(path, allow_pickle=False) as archive:
        return PillarModel(
            str(archive["model_id"]),
            tuple(str(p) for p in archive["pillars"]),
            archive["coef"],
            archive["intercept"],
        )
