from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from loupe.config import Exclusions
from loupe.index import connect
from loupe.models import MediaType
from loupe.scan import scan_items
from loupe.search import Facet, SearchError, SearchFilters, search
from loupe.vectors import VectorStore
from tests.fixtures import make_item, seed_vectors, synthetic_uuid
from tests.test_train import CONFIG

MODEL_ID = "fake/test"
DIM = 4
# Axis 0: beach. Axis 1: indoors. Axis 2: outdoors.
TEXTS = {
    "beach": [1, 0, 0, 0],
    "a photo taken indoors": [0, 1, 0, 0],
    "a photo taken outdoors": [0, 0, 1, 0],
}


class FakeText:
    model_id = MODEL_ID

    def encode_texts(self, texts: Sequence[str]) -> np.ndarray:
        rows = np.array([TEXTS.get(text, [0, 0, 0, 1]) for text in texts], dtype=np.float32)
        return rows


def _unit(*values: float) -> np.ndarray:
    vector = np.array(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


@pytest.fixture
def library(tmp_path: Path) -> tuple:
    beach_out, beach_in, video, person = (synthetic_uuid(n) for n in range(1, 5))
    items = [
        make_item(beach_out, persons=()),
        make_item(beach_in, persons=()),
        make_item(video, media_type=MediaType.VIDEO, persons=()),
        make_item(person, persons=("Test Person",), albums=("CC Food",)),
    ]
    connection = connect(tmp_path / "index.sqlite")
    scan_items(connection, items, Exclusions())
    store = VectorStore(tmp_path / "v.npy", DIM)
    seed_vectors(
        connection,
        store,
        MODEL_ID,
        {
            beach_out: _unit(0.9, 0.1, 0.4, 0),
            beach_in: _unit(0.8, 0.5, 0.1, 0),
            video: _unit(0.5, 0, 0.5, 0),
            person: _unit(0.1, 0.2, 0.2, 0.9),
        },
    )
    return connection, store, (beach_out, beach_in, video, person)


def test_ranks_by_text_similarity(library: tuple) -> None:
    connection, store, (beach_out, beach_in, video, _) = library
    hits = search(connection, store, FakeText(), "beach", SearchFilters(), CONFIG)
    assert [hit.uuid for hit in hits[:3]] == [beach_out, beach_in, video]


def test_filters_combine(library: tuple) -> None:
    connection, store, (beach_out, beach_in, video, person) = library
    text = FakeText()
    outdoor = SearchFilters(facets={Facet.SETTING: "outdoor"})
    assert beach_in not in {
        h.uuid for h in search(connection, store, text, "beach", outdoor, CONFIG)
    }
    videos = search(connection, store, text, "beach", SearchFilters(media_type="video"), CONFIG)
    assert [h.uuid for h in videos] == [video]
    by_person = search(connection, store, text, "", SearchFilters(person="Test Person"), CONFIG)
    assert [h.uuid for h in by_person] == [person]
    by_pillar = search(connection, store, text, "", SearchFilters(pillar="CC Food"), CONFIG)
    assert [h.uuid for h in by_pillar] == [person]


def test_unknown_facet_value_is_rejected(library: tuple) -> None:
    connection, store, _ = library
    with pytest.raises(SearchError):
        search(
            connection, store, FakeText(), "x", SearchFilters(facets={Facet.LIGHT: "neon"}), CONFIG
        )


def test_sheet_links_thumbnails_from_another_folder(tmp_path: Path) -> None:
    from PIL import Image

    from loupe.sheets import Section, Tile, write_sheet

    preview = tmp_path / "p.png"
    Image.new("RGB", (64, 64)).save(preview)
    sheet = write_sheet(
        tmp_path / "search" / "beach.html",
        "beach",
        [Section("Results", [Tile(synthetic_uuid(1), str(preview), "photo")], 1)],
        tmp_path / "review" / "thumbs",
    )
    assert "../review/thumbs/" in sheet.read_text()
