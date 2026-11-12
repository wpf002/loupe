from datetime import date
from pathlib import Path

import pytest

from loupe.config import ConfigError, load_pillar_config, merge_config

BASE = """
label_album_prefixes = ["CC "]
include_albums = ["Hotel Collaboration"]
exclude_albums = []
min_confidence = 0.80
review_floor = 0.50
min_examples_per_pillar = 30

[exclusions]
albums = ["Private"]
persons = ["Test Person"]
date_ranges = [{ start = 2021-06-01, end = 2021-06-30 }]
uuids = ["00000000-0000-4000-8000-00000000000a"]
"""


def _write(directory: Path, name: str, text: str) -> None:
    (directory / name).write_text(text)


def test_loads_base_config(tmp_path: Path) -> None:
    _write(tmp_path, "pillars.toml", BASE)
    config = load_pillar_config(tmp_path)
    assert config.exclusions.albums == {"Private"}
    assert config.exclusions.date_ranges[0].start == date(2021, 6, 1)
    assert config.exclusions.uuids == {"00000000-0000-4000-8000-00000000000A"}


def test_local_file_overrides_base(tmp_path: Path) -> None:
    _write(tmp_path, "pillars.toml", BASE)
    _write(
        tmp_path,
        "pillars.local.toml",
        'label_album_prefixes = []\ninclude_albums = ["Stand In"]\n[exclusions]\npersons = []\n',
    )
    config = load_pillar_config(tmp_path)
    assert config.is_pillar_album("Stand In")
    assert not config.is_pillar_album("CC Travel")
    assert config.exclusions.persons == frozenset()
    assert config.exclusions.albums == {"Private"}


def test_merge_keeps_unrelated_table_keys() -> None:
    merged = merge_config({"a": 1, "t": {"x": 1, "y": 2}}, {"t": {"y": 3}})
    assert merged == {"a": 1, "t": {"x": 1, "y": 3}}


def test_excluded_album_is_never_a_pillar(tmp_path: Path) -> None:
    _write(tmp_path, "pillars.toml", BASE.replace('albums = ["Private"]', 'albums = ["CC Kids"]'))
    assert not load_pillar_config(tmp_path).is_pillar_album("CC Kids")


@pytest.mark.parametrize(
    ("find", "replace_with"),
    [
        ("end = 2021-06-30", "end = 2021-05-01"),
        ('uuids = ["00000000-0000-4000-8000-00000000000a"]', 'uuids = ["not-a-uuid"]'),
        ("review_floor = 0.50", "review_floor = 0.90"),
        ('albums = ["Private"]', 'albums = ["  "]'),
        ("[exclusions]", "[exclusions]\nfaces = []"),
    ],
)
def test_rejects_invalid_config(tmp_path: Path, find: str, replace_with: str) -> None:
    _write(tmp_path, "pillars.toml", BASE.replace(find, replace_with))
    with pytest.raises(ConfigError):
        load_pillar_config(tmp_path)


def test_missing_config_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_pillar_config(tmp_path)


def test_repo_config_is_valid() -> None:
    config = load_pillar_config(Path(__file__).parent.parent / "config")
    assert config.min_confidence > config.review_floor
