"""M4: score items outside her pillar albums. An item may clear the bar for several pillars."""

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from loupe.config import PillarConfig
from loupe.train import PillarModel, embedded_rows, pillar_members
from loupe.vectors import VectorStore

SCORE_BATCH = 4096


@dataclass
class ClassifyResult:
    scored: int = 0
    purged: int = 0
    confident: dict[str, int] = field(default_factory=dict)
    borderline: dict[str, int] = field(default_factory=dict)


def purge_stale_scores(connection: sqlite3.Connection, model: PillarModel) -> int:
    """Drop scores of items excluded since, and of pillars the model no longer has."""
    placeholders = ",".join("?" for _ in model.pillars)
    with connection:
        excluded = connection.execute(
            """
            DELETE FROM pillar_scores WHERE uuid IN (
                SELECT uuid FROM items WHERE excluded_reason IS NOT NULL
                   OR is_screenshot = 1 OR is_screen_recording = 1 OR is_burst = 1)
            """
        ).rowcount
        # Only "?" placeholders are interpolated; pillar names are bound parameters.
        dropped = (
            connection.execute(
                f"DELETE FROM pillar_scores WHERE pillar NOT IN ({placeholders})",  # noqa: S608
                model.pillars,
            ).rowcount
            if model.pillars
            else connection.execute("DELETE FROM pillar_scores").rowcount
        )
    return excluded + dropped


def classify_items(
    connection: sqlite3.Connection,
    store: VectorStore,
    model: PillarModel,
    config: PillarConfig,
    limit: int | None = None,
) -> ClassifyResult:
    result = ClassifyResult(purged=purge_stale_scores(connection, model))
    labeled = set(pillar_members(connection, config))
    rows = {u: r for u, r in embedded_rows(connection, model.model_id).items() if u not in labeled}
    uuids = list(rows)[:limit] if limit is not None else list(rows)
    now = datetime.now(UTC).isoformat()
    for start in range(0, len(uuids), SCORE_BATCH):
        batch = uuids[start : start + SCORE_BATCH]
        probabilities = model.probabilities(store.read([rows[uuid] for uuid in batch]))
        records = [
            (uuid, pillar, float(probabilities[i, j]), now)
            for i, uuid in enumerate(batch)
            for j, pillar in enumerate(model.pillars)
            if probabilities[i, j] >= config.review_floor
        ]
        with connection:
            connection.executemany(
                "DELETE FROM pillar_scores WHERE uuid = ?", [(uuid,) for uuid in batch]
            )
            connection.executemany(
                "INSERT INTO pillar_scores (uuid, pillar, probability, scored_at) "
                "VALUES (?, ?, ?, ?)",
                records,
            )
        result.scored += len(batch)
    for pillar in model.pillars:
        result.confident[pillar] = count_picks(connection, pillar, config.min_confidence, 1.01)
        result.borderline[pillar] = count_picks(
            connection, pillar, config.review_floor, config.min_confidence
        )
    return result


def count_picks(connection: sqlite3.Connection, pillar: str, low: float, high: float) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM pillar_scores WHERE pillar = ? AND probability >= ? "
        "AND probability < ?",
        (pillar, low, high),
    ).fetchone()
    return int(row[0])


@dataclass(frozen=True)
class Pick:
    uuid: str
    probability: float
    preview_path: str | None
    media_type: str


def picks(
    connection: sqlite3.Connection, pillar: str, low: float, high: float, limit: int | None
) -> list[Pick]:
    rows = connection.execute(
        """
        SELECT pillar_scores.uuid, probability, preview_path, media_type FROM pillar_scores
        JOIN items ON items.uuid = pillar_scores.uuid
        WHERE pillar = ? AND probability >= ? AND probability < ? AND excluded_reason IS NULL
        ORDER BY probability DESC, pillar_scores.uuid
        LIMIT ?
        """,
        (pillar, low, high, -1 if limit is None else limit),
    )
    return [Pick(*row) for row in rows]
