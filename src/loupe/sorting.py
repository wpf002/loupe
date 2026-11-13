"""Library and Search for Loupe.app: embed, train and classify as one background job,
album writes as a second job the user starts, and text search over the vectors.

Same stages and rules as `loupe embed`, `train`, `classify`, `write-albums` and `search`.
The album job is the app's `--apply`: `status()` is the dry run, with per-album counts.
"""

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import Protocol

from loupe.albums import AlbumWriter, album_name, apply_plans, plan_albums
from loupe.classify import classify_items, count_picks
from loupe.config import PillarConfig
from loupe.embed import ImageEncoder, embed_items, pending_work, vector_path
from loupe.index import connect, eligible_uuids
from loupe.search import Facet, SearchError, SearchFilters, SearchHit, TextEncoder, search
from loupe.train import (
    PillarModel,
    PillarScore,
    TrainingError,
    load_model,
    load_training_set,
    model_path,
    pillar_members,
    save_model,
    train_pillars,
)
from loupe.vectors import VectorStore, VectorStoreError

SORT_FAILED = "Sorting stopped. Try again."
ALBUMS_FAILED = "Adding to Photos stopped. Try again."
NOT_SORTED = "Sort the library first."
ENCODER_BUSY = "Loupe is reading photos. Search again when that finishes."
JOB_RUNNING = "Loupe is already sorting."
MEDIA_TYPES = frozenset({"photo", "video"})

logger = logging.getLogger("loupe.sorting")


class SortEncoder(ImageEncoder, TextEncoder, Protocol):
    """SigLIP in the app, a fake in tests."""


class Stage(StrEnum):
    IDLE = "idle"
    EMBED = "embed"
    TRAIN = "train"
    CLASSIFY = "classify"
    ALBUMS = "albums"
    DONE = "done"
    FAILED = "failed"


RUNNING = frozenset({Stage.EMBED, Stage.TRAIN, Stage.CLASSIFY, Stage.ALBUMS})


@dataclass(frozen=True)
class Progress:
    stage: Stage = Stage.IDLE
    done: int = 0
    total: int = 0
    error: str | None = None


class SortError(Exception):
    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.CONFLICT) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class PillarRow:
    pillar: str
    album: str
    examples: int
    trained: bool
    precision: float | None
    recall: float | None
    confident: int
    borderline: int
    in_album: int
    to_add: int


def example_counts(connection: sqlite3.Connection, config: PillarConfig) -> dict[str, int]:
    counts: dict[str, int] = {}
    for pillars in pillar_members(connection, config).values():
        for pillar in pillars:
            counts[pillar] = counts.get(pillar, 0) + 1
    return dict(sorted(counts.items()))


def embedded_count(connection: sqlite3.Connection, model_id: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM embeddings JOIN items ON items.uuid = embeddings.uuid "
        "WHERE embeddings.model = ? AND items.excluded_reason IS NULL",
        (model_id,),
    ).fetchone()
    return int(row[0])


def pillar_rows(
    connection: sqlite3.Connection,
    config: PillarConfig,
    model: PillarModel | None,
    scores: dict[str, PillarScore],
) -> list[PillarRow]:
    trained = model.pillars if model is not None else ()
    plans = {plan.pillar: plan for plan in plan_albums(connection, trained, config)}
    rows = []
    for pillar, examples in example_counts(connection, config).items():
        plan = plans.get(pillar)
        score = scores.get(pillar)
        rows.append(
            PillarRow(
                pillar=pillar,
                album=album_name(pillar, config),
                examples=examples,
                trained=plan is not None,
                precision=score.precision if score else None,
                recall=score.recall if score else None,
                confident=plan.picks if plan else 0,
                borderline=(
                    count_picks(connection, pillar, config.review_floor, config.min_confidence)
                    if plan
                    else 0
                ),
                in_album=plan.already_written if plan else 0,
                to_add=len(plan.to_add) if plan else 0,
            )
        )
    return rows


class Sorter:
    """One job at a time. Safe to call from the server's request threads."""

    def __init__(
        self,
        data_dir: Path,
        index_path: Path,
        config: PillarConfig,
        encoder_factory: Callable[[], SortEncoder],
        writer_factory: Callable[[], AlbumWriter],
    ) -> None:
        self.data_dir = data_dir
        self.index_path = index_path
        self.config = config
        self._encoder_factory = encoder_factory
        self._writer_factory = writer_factory
        self._lock = threading.Lock()
        self._encoder_lock = threading.Lock()
        self._encoder: SortEncoder | None = None
        self._progress = Progress()
        self._scores: dict[str, PillarScore] = {}

    # State -----------------------------------------------------------------------

    def progress(self) -> Progress:
        with self._lock:
            return self._progress

    def _set(self, **changes: object) -> None:
        with self._lock:
            self._progress = replace(self._progress, **changes)  # type: ignore[arg-type]

    def _advance(self, count: int) -> None:
        with self._lock:
            self._progress = replace(self._progress, done=self._progress.done + count)

    def _encoder_instance(self) -> SortEncoder:
        """Loads SigLIP once. Callers hold _encoder_lock."""
        if self._encoder is None:
            self._encoder = self._encoder_factory()
        return self._encoder

    def _model(self) -> PillarModel | None:
        path = model_path(self.data_dir)
        return load_model(path) if path.is_file() else None

    def status(self) -> dict[str, object]:
        progress = self.progress()
        model = self._model()
        connection = connect(self.index_path)
        try:
            items = len(eligible_uuids(connection))
            embedded = embedded_count(connection, model.model_id) if model else None
            if embedded is None and self._encoder is not None:
                embedded = embedded_count(connection, self._encoder.model_id)
            rows = pillar_rows(connection, self.config, model, self._scores_copy())
        finally:
            connection.close()
        return {
            "stage": progress.stage.value,
            "done": progress.done,
            "total": progress.total,
            "error": progress.error,
            "items": items,
            "embedded": embedded or 0,
            "sorted": model is not None,
            "min_examples": self.config.min_examples_per_pillar,
            "pillars": [row.__dict__ for row in rows],
        }

    def _scores_copy(self) -> dict[str, PillarScore]:
        with self._lock:
            return dict(self._scores)

    # Jobs ------------------------------------------------------------------------

    def _claim(self, stage: Stage, total: int = 0) -> None:
        with self._lock:
            if self._progress.stage in RUNNING:
                raise SortError(JOB_RUNNING)
            self._progress = Progress(stage=stage, total=total)

    def _launch(self, name: str, job: Callable[[], None]) -> None:
        threading.Thread(target=job, name=name, daemon=True).start()

    def start_sort(self, limit: int | None = None) -> None:
        """Embed what's missing, retrain on her albums, rescore everything else."""
        self._claim(Stage.EMBED)
        self._launch("loupe-sort", lambda: self._run_sort(limit))

    def start_albums(self) -> None:
        """Adds confident picks to albums in the Loupe folder. Nothing else in Photos changes."""
        model = self._model()
        if model is None:
            raise SortError(NOT_SORTED, HTTPStatus.BAD_REQUEST)
        self._claim(Stage.ALBUMS)
        self._launch("loupe-albums", lambda: self._run_albums(model))

    def _run_sort(self, limit: int | None) -> None:
        connection = connect(self.index_path)
        try:
            with self._encoder_lock:
                encoder = self._encoder_instance()
                self._set(total=len(pending_work(connection, encoder.model_id, limit)))
                store = VectorStore(vector_path(self.data_dir, encoder.model_id), encoder.dim)
                embedded = embed_items(connection, store, encoder, limit, on_batch=self._advance)
            skipped = sum(embedded.skipped.values())
            logger.info("embedded %d, skipped %d", embedded.embedded, skipped)
            self._set(stage=Stage.TRAIN, done=0, total=0)
            training_set = load_training_set(connection, store, self.config, encoder.model_id)
            trained = train_pillars(training_set, self.config, encoder.model_id)
            save_model(trained.model, model_path(self.data_dir))
            with self._lock:
                self._scores = {score.pillar: score for score in trained.scores}
            logger.info("trained %d pillars", len(trained.scores))
            self._set(stage=Stage.CLASSIFY)
            classified = classify_items(connection, store, trained.model, self.config)
            logger.info("scored %d items", classified.scored)
            self._set(stage=Stage.DONE)
        except TrainingError as error:
            logger.info("training stopped: %s", error)
            self._set(stage=Stage.FAILED, error=too_few_examples(self.config))
        except Exception:
            logger.exception("sort failed")
            self._set(stage=Stage.FAILED, error=SORT_FAILED)
        finally:
            connection.close()

    def _run_albums(self, model: PillarModel) -> None:
        connection = connect(self.index_path)
        try:
            plans = plan_albums(connection, model.pillars, self.config)
            self._set(total=sum(len(plan.to_add) for plan in plans))
            writer = self._writer_factory()
            for _album, added in apply_plans(connection, plans, writer):
                self._advance(added)
                logger.info("added %d to an album", added)
            self._set(stage=Stage.DONE)
        except Exception:
            logger.exception("album write failed")
            self._set(stage=Stage.FAILED, error=ALBUMS_FAILED)
        finally:
            connection.close()

    # Search ----------------------------------------------------------------------

    def search(
        self,
        query: str,
        pillar: str | None,
        media_type: str | None,
        facets: dict[Facet, str],
    ) -> list[SearchHit]:
        if media_type is not None and media_type not in MEDIA_TYPES:
            raise SortError("Pick photos or videos.", HTTPStatus.BAD_REQUEST)
        # Embedding holds the encoder and grows the vector file, so search waits for it.
        if not self._encoder_lock.acquire(blocking=False):
            raise SortError(ENCODER_BUSY)
        connection = connect(self.index_path)
        try:
            if pillar is not None and pillar not in example_counts(connection, self.config):
                raise SortError("No such pillar.", HTTPStatus.BAD_REQUEST)
            encoder = self._encoder_instance()
            store = VectorStore(vector_path(self.data_dir, encoder.model_id))
            filters = SearchFilters(pillar=pillar, media_type=media_type, facets=facets or None)
            return search(connection, store, encoder, query, filters, self.config)
        except VectorStoreError as error:
            raise SortError(NOT_SORTED, HTTPStatus.BAD_REQUEST) from error
        except SearchError as error:
            raise SortError(str(error), HTTPStatus.BAD_REQUEST) from error
        finally:
            connection.close()
            self._encoder_lock.release()


def too_few_examples(config: PillarConfig) -> str:
    return f"Each pillar album needs {config.min_examples_per_pillar} photos before Loupe can sort."
