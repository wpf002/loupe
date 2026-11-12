from pathlib import Path

import numpy as np
from PIL import Image

from loupe.classify import classify_items, picks
from loupe.config import Exclusions
from loupe.review import write_review
from loupe.scan import scan_items
from loupe.train import load_training_set, train_pillars
from tests.fixtures import make_item, seed_vectors, synthetic_uuid
from tests.test_train import CONFIG, MODEL_ID, _cluster, build_library

TRAVEL_LOOKALIKES = 5


def _trained(tmp_path: Path) -> tuple:
    connection, store = build_library(tmp_path)
    model = train_pillars(
        load_training_set(connection, store, CONFIG, MODEL_ID), CONFIG, MODEL_ID
    ).model
    rng = np.random.default_rng(1)
    lookalikes = {synthetic_uuid(9000 + n): _cluster(rng, 0) for n in range(TRAVEL_LOOKALIKES)}
    scan_items(connection, [make_item(uuid, albums=()) for uuid in lookalikes], Exclusions())
    first_row = connection.execute("SELECT MAX(vector_row) + 1 FROM embeddings").fetchone()[0]
    seed_vectors(connection, store, MODEL_ID, lookalikes, first_row)
    return connection, store, model


def test_unlabeled_items_get_scored_and_labeled_ones_do_not(tmp_path: Path) -> None:
    connection, store, model = _trained(tmp_path)
    result = classify_items(connection, store, model, CONFIG)
    # 80 unlabeled + 5 hotel items (hotel is not a trained pillar, so they stay unlabeled).
    assert result.scored == 85
    labeled = {row[0] for row in connection.execute("SELECT uuid FROM item_albums")}
    scored = {row[0] for row in connection.execute("SELECT uuid FROM pillar_scores")}
    hotel = {
        row[0]
        for row in connection.execute("SELECT uuid FROM item_albums WHERE album = 'CC Hotel'")
    }
    assert scored.isdisjoint(labeled - hotel)


def test_rerun_is_stable_and_exclusions_purge(tmp_path: Path) -> None:
    connection, store, model = _trained(tmp_path)
    classify_items(connection, store, model, CONFIG)
    before = connection.execute("SELECT COUNT(*) FROM pillar_scores").fetchone()[0]
    classify_items(connection, store, model, CONFIG)
    assert connection.execute("SELECT COUNT(*) FROM pillar_scores").fetchone()[0] == before
    uuid = connection.execute("SELECT uuid FROM pillar_scores LIMIT 1").fetchone()[0]
    scan_items(
        connection,
        [make_item(uuid, albums=("Private",))],
        Exclusions(albums=frozenset({"Private"})),
    )
    result = classify_items(connection, store, model, CONFIG)
    assert result.purged >= 1
    assert (
        connection.execute("SELECT COUNT(*) FROM pillar_scores WHERE uuid = ?", (uuid,)).fetchone()[
            0
        ]
        == 0
    )


def test_review_pages_render_with_thumbnails(tmp_path: Path) -> None:
    connection, store, model = _trained(tmp_path)
    preview = tmp_path / "preview.png"
    Image.new("RGB", (900, 600), (90, 120, 160)).save(preview)
    connection.execute("UPDATE items SET preview_path = ?", (str(preview),))
    # Force one strong travel pick regardless of synthetic noise.
    connection.execute(
        "INSERT OR REPLACE INTO pillar_scores VALUES "
        "((SELECT uuid FROM items WHERE uuid NOT IN (SELECT uuid FROM item_albums) LIMIT 1),"
        " 'CC Travel', 0.97, 'now')"
    )
    connection.commit()
    pages = write_review(connection, model.pillars, CONFIG, tmp_path / "review")
    travel = pages.pillars["CC Travel"].read_text()
    assert "Confident" in travel and "97%" in travel
    assert any((tmp_path / "review" / "thumbs").iterdir())
    assert "CC Food" in pages.index.read_text()
    assert len(picks(connection, "CC Travel", 0.9, 1.01, None)) >= 1
    assert np.isfinite(model.coef).all()
