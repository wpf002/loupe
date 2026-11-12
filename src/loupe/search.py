"""M6: text search over the M2 embeddings, with filters and zero-shot facets."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import numpy as np

from loupe.config import PillarConfig
from loupe.vectors import VectorStore

DEFAULT_RESULTS = 60


class Facet(StrEnum):
    LIGHT = "light"
    SHOT = "shot"
    SETTING = "setting"


# Each value is scored against the others in its facet; the best match wins.
FACET_PROMPTS: dict[Facet, dict[str, str]] = {
    Facet.LIGHT: {
        "natural": "a photo lit by natural daylight",
        "artificial": "a photo lit by artificial lamps or indoor lighting",
    },
    Facet.SHOT: {
        "close-up": "a close-up photo of a face or a small object",
        "medium": "a medium shot of a person from the waist up",
        "wide": "a wide shot showing a whole scene or landscape",
        "selfie": "a selfie taken at arm's length with a phone",
    },
    Facet.SETTING: {
        "indoor": "a photo taken indoors",
        "outdoor": "a photo taken outdoors",
    },
}


class TextEncoder(Protocol):
    model_id: str

    def encode_texts(self, texts: Sequence[str]) -> np.ndarray: ...


@dataclass(frozen=True)
class SearchFilters:
    pillar: str | None = None
    media_type: str | None = None
    person: str | None = None
    facets: dict[Facet, str] | None = None


@dataclass(frozen=True)
class SearchHit:
    uuid: str
    score: float
    preview_path: str | None
    media_type: str


class SearchError(Exception):
    """Raised for a filter value that doesn't exist."""


def validate_facets(facets: dict[Facet, str]) -> None:
    for facet, value in facets.items():
        if value not in FACET_PROMPTS[facet]:
            options = ", ".join(FACET_PROMPTS[facet])
            raise SearchError(f"{facet} must be one of: {options}")


def _candidates(
    connection: sqlite3.Connection, model_id: str, filters: SearchFilters, config: PillarConfig
) -> list[tuple[str, int, str | None, str]]:
    clauses = [
        "embeddings.model = ?",
        "items.excluded_reason IS NULL",
        "items.is_screenshot = 0",
        "items.is_screen_recording = 0",
    ]
    params: list[object] = [model_id]
    if filters.media_type is not None:
        clauses.append("items.media_type = ?")
        params.append(filters.media_type)
    if filters.person is not None:
        clauses.append("items.uuid IN (SELECT uuid FROM item_persons WHERE person = ?)")
        params.append(filters.person)
    if filters.pillar is not None:
        clauses.append(
            "(items.uuid IN (SELECT uuid FROM item_albums WHERE album = ?)"
            " OR items.uuid IN (SELECT uuid FROM pillar_scores"
            " WHERE pillar = ? AND probability >= ?))"
        )
        params.extend([filters.pillar, filters.pillar, config.min_confidence])
    # Clauses are fixed strings chosen above; every value is a bound parameter.
    sql = (
        "SELECT items.uuid, embeddings.vector_row, items.preview_path, items.media_type "  # noqa: S608
        "FROM embeddings JOIN items ON items.uuid = embeddings.uuid WHERE "
        + " AND ".join(clauses)
        + " ORDER BY items.uuid"
    )
    return connection.execute(sql, params).fetchall()


def facet_mask(vectors: np.ndarray, encoder: TextEncoder, facets: dict[Facet, str]) -> np.ndarray:
    """True where every requested facet's best-matching value is the requested one."""
    keep = np.ones(len(vectors), dtype=bool)
    for facet, wanted in facets.items():
        values = list(FACET_PROMPTS[facet])
        prompts = encoder.encode_texts([FACET_PROMPTS[facet][value] for value in values])
        best = np.argmax(vectors @ prompts.T, axis=1)
        keep &= best == values.index(wanted)
    return keep


def search(
    connection: sqlite3.Connection,
    store: VectorStore,
    encoder: TextEncoder,
    query: str,
    filters: SearchFilters,
    config: PillarConfig,
    results: int = DEFAULT_RESULTS,
) -> list[SearchHit]:
    if filters.facets:
        validate_facets(filters.facets)
    rows = _candidates(connection, encoder.model_id, filters, config)
    if not rows:
        return []
    vectors = store.read([row[1] for row in rows]).astype(np.float32)
    scores = vectors @ encoder.encode_texts([query])[0] if query.strip() else np.zeros(len(rows))
    if filters.facets:
        scores = np.where(facet_mask(vectors, encoder, filters.facets), scores, -np.inf)
    order = np.argsort(-scores, kind="stable")[:results]
    return [
        SearchHit(rows[i][0], float(scores[i]), rows[i][2], rows[i][3])
        for i in order
        if np.isfinite(scores[i])
    ]
