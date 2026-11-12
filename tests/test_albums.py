from pathlib import Path

import pytest

from loupe.albums import album_name, apply_plans, plan_albums
from tests.test_classify import TRAVEL_LOOKALIKES, _trained
from tests.test_train import CONFIG


class FakeWriter:
    def __init__(self, fail_after: int | None = None) -> None:
        self.albums: dict[str, list[str]] = {}
        self.fail_after = fail_after

    def ensure_album(self, album: str) -> None:
        self.albums.setdefault(album, [])

    def add(self, album: str, uuids: list[str]) -> int:
        if self.fail_after is not None and len(self.albums[album]) >= self.fail_after:
            raise RuntimeError("Photos stopped responding")
        self.albums[album].extend(uuids)
        return len(uuids)


def _classified(tmp_path: Path) -> tuple:
    from loupe.classify import classify_items

    connection, store, model = _trained(tmp_path)
    classify_items(connection, store, model, CONFIG)
    return connection, model


def test_album_names_drop_the_label_prefix() -> None:
    config = CONFIG.model_copy(update={"include_albums": frozenset({"Hotel Collaboration"})})
    assert album_name("CC Travel", config) == "Travel"
    assert album_name("Hotel Collaboration", config) == "Hotel Collaboration"
    assert album_name("CC ", CONFIG) == "CC"


def test_dry_run_plan_writes_nothing(tmp_path: Path) -> None:
    connection, model = _classified(tmp_path)
    plans = plan_albums(connection, model.pillars, CONFIG)
    travel = next(plan for plan in plans if plan.album == "Travel")
    assert len(travel.to_add) == TRAVEL_LOOKALIKES
    assert connection.execute("SELECT COUNT(*) FROM album_writes").fetchone()[0] == 0


def test_apply_resumes_after_a_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("loupe.albums.ADD_BATCH", 2)
    connection, model = _classified(tmp_path)
    crashing = FakeWriter(fail_after=2)
    with pytest.raises(RuntimeError):
        list(apply_plans(connection, plan_albums(connection, model.pillars, CONFIG), crashing))
    writer = FakeWriter()
    list(apply_plans(connection, plan_albums(connection, model.pillars, CONFIG), writer))
    assert len(crashing.albums["Travel"]) + len(writer.albums["Travel"]) == TRAVEL_LOOKALIKES
    again = plan_albums(connection, model.pillars, CONFIG)
    assert all(not plan.to_add for plan in again)
