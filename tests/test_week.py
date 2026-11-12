from datetime import date
from pathlib import Path

import pytest

from loupe.claude import ClaudeError, Request
from loupe.index import connect
from loupe.week import (
    DAYS,
    PlannedPost,
    PostFormat,
    TakeSummary,
    WeekPlan,
    latest_week,
    load_week,
    pillar_counts,
    plan_week,
    validate_plan,
    week_request,
    week_start,
)
from tests.fixtures import UUID_A, UUID_B, make_brand

PILLARS = {"CC Travel": 40, "CC Food": 25, "CC Home": 12}
TAKES = [TakeSummary(UUID_A, "Three things I never pack")]
MONDAY = date(2026, 11, 16)
FORMATS = (PostFormat.REEL, PostFormat.CAROUSEL, PostFormat.STORY)


def _post(weekday: str, number: int, **overrides: object) -> PlannedPost:
    post_format = FORMATS[number % 3]
    values: dict[str, object] = {
        "day": weekday,
        "pillar": list(PILLARS)[number % 3],
        "format": post_format,
        "angle": f"Angle {number}",
        "asset_query": "beach at sunset",
        "caption": f"Caption {number}",
        "slides": ["a", "b", "c"] if post_format is PostFormat.CAROUSEL else [],
    }
    values.update(overrides)
    return PlannedPost.model_validate(values)


def _plan(**first_overrides: object) -> WeekPlan:
    posts = [_post(day, number) for number, day in enumerate(DAYS)]
    posts[0] = _post("Mon", 0, **first_overrides)
    return WeekPlan(posts=list(reversed(posts)))


class FakeClient:
    def __init__(self, plan: WeekPlan) -> None:
        self.plan = plan
        self.requests: list[Request] = []

    def parse(self, request: Request, output_type: type[WeekPlan]) -> WeekPlan:
        self.requests.append(request)
        return self.plan


@pytest.fixture
def connection(tmp_path: Path):
    connection = connect(tmp_path / "index.sqlite")
    yield connection
    connection.close()


def test_week_start_is_next_monday() -> None:
    assert week_start(date(2026, 11, 12)) == MONDAY
    assert week_start(MONDAY) == MONDAY


def test_pillar_counts_sorted_by_size() -> None:
    memberships = {"u1": {"CC Food"}, "u2": {"CC Food", "CC Home"}}
    assert pillar_counts(memberships) == {"CC Food": 2, "CC Home": 1}


def test_request_has_no_uuids(tmp_path: Path) -> None:
    request = week_request(MONDAY, make_brand(tmp_path), PILLARS, TAKES)
    assert "1. Three things I never pack" in request.prompt
    assert UUID_A not in request.prompt
    assert "Comment GUIDE" in request.prompt


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"day": "Tue"}, "each day once"),
        ({"pillar": "CC Unknown"}, "unknown pillar"),
        ({"take": 4}, "not listed"),
        ({"format": PostFormat.CAROUSEL, "slides": ["only"]}, "1 slides"),
    ],
)
def test_validate_plan_rejects(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ClaudeError, match=message):
        validate_plan(_plan(**overrides), list(PILLARS), len(TAKES))


def test_plan_week_orders_days_assigns_assets_and_stores(connection, tmp_path: Path) -> None:
    client = FakeClient(_plan(take=1))
    searched: list[str] = []

    def finder(post: PlannedPost) -> list[str]:
        searched.append(post.day)
        return [UUID_B] * 10

    week = plan_week(connection, MONDAY, make_brand(tmp_path), PILLARS, TAKES, client, finder)
    assert [post.day for post in week.plan.posts] == list(DAYS)
    assert week.assets[0] == [UUID_A]
    assert len(week.assets[1]) == 6
    assert "Mon" not in searched
    stored = load_week(connection, MONDAY)
    assert stored == week
    assert latest_week(connection) == week


def test_plan_week_needs_pillars(connection, tmp_path: Path) -> None:
    with pytest.raises(ClaudeError, match="no pillar"):
        plan_week(
            connection, MONDAY, make_brand(tmp_path), {}, [], FakeClient(_plan()), lambda _: []
        )
