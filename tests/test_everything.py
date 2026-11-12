from datetime import date
from pathlib import Path

import pytest
from PIL import Image

from loupe.brand import hex_to_rgb
from loupe.claude import ClaudeError, Request
from loupe.index import connect
from loupe.newsletter import (
    MAX_SUBJECT_CHARACTERS,
    EmailDraft,
    email_for_week,
    email_request,
    email_text,
)
from loupe.story import STORY_CANVAS, render_story, story_dir
from loupe.week import DAYS, PlannedPost, PlannedWeek, PostFormat, WeekPlan
from tests.fixtures import SYSTEM_FONT, UUID_A, make_brand

MONDAY = date(2026, 11, 16)
DRAFT = EmailDraft(subject="Packing for two days", preview="What stays home", body="Hi.\n\nOne.")


def _week() -> PlannedWeek:
    posts = [
        PlannedPost(
            day=day,
            pillar="CC Travel",
            format=PostFormat.STORY,
            angle=f"Angle {number}",
            asset_query="suitcase",
            caption=f"Caption {number}",
            story_frames=["One", "Two", "Three"],
        )
        for number, day in enumerate(DAYS)
    ]
    return PlannedWeek(
        start=MONDAY, plan=WeekPlan(posts=posts), take_uuids={}, assets={0: [UUID_A]}
    )


class FakeClient:
    def __init__(self, draft: EmailDraft) -> None:
        self.draft = draft
        self.requests: list[Request] = []

    def parse(self, request: Request, output_type: type[EmailDraft]) -> EmailDraft:
        self.requests.append(request)
        return self.draft


@pytest.fixture
def connection(tmp_path: Path):
    connection = connect(tmp_path / "index.sqlite")
    yield connection
    connection.close()


def test_email_request_carries_captions_not_uuids(tmp_path: Path) -> None:
    request = email_request(_week(), make_brand(tmp_path))
    assert "Caption 3" in request.prompt
    assert UUID_A not in request.prompt
    assert request.images == ()


def test_email_is_stored_and_reused(connection, tmp_path: Path) -> None:
    client = FakeClient(DRAFT)
    brand = make_brand(tmp_path)
    first = email_for_week(connection, _week(), brand, client)
    second = email_for_week(connection, _week(), brand, client)
    assert first == second == DRAFT
    assert len(client.requests) == 1
    email_for_week(connection, _week(), brand, client, fresh=True)
    assert len(client.requests) == 2


def test_email_rejects_long_subject(connection, tmp_path: Path) -> None:
    long = DRAFT.model_copy(update={"subject": "x" * (MAX_SUBJECT_CHARACTERS + 1)})
    with pytest.raises(ClaudeError, match="subject"):
        email_for_week(connection, _week(), make_brand(tmp_path), FakeClient(long))


def test_email_text_has_headers() -> None:
    assert email_text(DRAFT).startswith("Subject: Packing for two days\nPreview: What stays home")


@pytest.mark.skipif(not SYSTEM_FONT.is_file(), reason="stock macOS font missing")
def test_story_frames_render_at_9_by_16(tmp_path: Path) -> None:
    brand = make_brand(tmp_path)
    target = story_dir(tmp_path / "renders", MONDAY.isoformat(), "Wed")
    paths = render_story(brand, ["Open", "Middle", "Comment GUIDE"], None, target)
    assert [path.name for path in paths] == ["frame-01.png", "frame-02.png", "frame-03.png"]
    assert target.name == "2026-11-16-wed"
    with Image.open(paths[1]) as frame:
        assert frame.size == STORY_CANVAS.size
        assert frame.getpixel((10, STORY_CANVAS.height - 10)) == hex_to_rgb(brand.colors.paper)
        top_band = frame.crop((0, 0, STORY_CANVAS.width, STORY_CANVAS.top - 20))
        assert top_band.getcolors() == [
            (top_band.width * top_band.height, hex_to_rgb(brand.colors.paper))
        ]
