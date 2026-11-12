from datetime import UTC, date, datetime

import pytest

from loupe.config import DateRange, Exclusions
from loupe.exclusions import exclusion_reason
from loupe.models import ExclusionReason
from tests.fixtures import UUID_A, UUID_B, make_item

EXCLUSIONS = Exclusions(
    albums=frozenset({"Private"}),
    persons=frozenset({"Off Limits"}),
    date_ranges=(DateRange(start=date(2021, 6, 1), end=date(2021, 6, 30)),),
    uuids=frozenset({UUID_B}),
)


def test_eligible_item_has_no_reason() -> None:
    assert exclusion_reason(make_item(), EXCLUSIONS) is None


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"is_hidden": True}, ExclusionReason.HIDDEN),
        ({"uuid": UUID_B}, ExclusionReason.UUID),
        ({"uuid": UUID_B.lower()}, ExclusionReason.UUID),
        ({"albums": ("CC Travel", "Private")}, ExclusionReason.ALBUM),
        ({"persons": ("Test Person", "Off Limits")}, ExclusionReason.PERSON),
        ({"taken_at": datetime(2021, 6, 1, 0, 0, tzinfo=UTC)}, ExclusionReason.DATE),
        ({"taken_at": datetime(2021, 6, 30, 23, 59, tzinfo=UTC)}, ExclusionReason.DATE),
    ],
)
def test_each_rule_excludes(overrides: dict[str, object], expected: ExclusionReason) -> None:
    assert exclusion_reason(make_item(**overrides), EXCLUSIONS) == expected


def test_dates_outside_range_are_eligible() -> None:
    item = make_item(taken_at=datetime(2021, 7, 1, tzinfo=UTC))
    assert exclusion_reason(item, EXCLUSIONS) is None


def test_missing_date_is_not_date_excluded() -> None:
    assert exclusion_reason(make_item(taken_at=None), EXCLUSIONS) is None


def test_album_match_is_exact() -> None:
    item = make_item(uuid=UUID_A, albums=("Private Stuff",))
    assert exclusion_reason(item, EXCLUSIONS) is None
