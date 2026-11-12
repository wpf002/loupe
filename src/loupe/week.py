"""M11: Claude plans Monday to Sunday across her pillars and formats, drafts captions, and
each post gets assets from the M6 image bank.

Sends her brand voice, pillar names and the opening lines of her talking takes. No
previews, UUIDs, people or places go to Claude. Plans are stored by week start date.
"""

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from loupe.brand import BrandProfile
from loupe.claude import MODEL, ClaudeError, Request, StructuredClient
from loupe.hooks import voice_brief
from loupe.transcribe import load_transcript

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DayName = Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MIN_SLIDES = 3
MAX_SLIDES = 10
MAX_TAKES_OFFERED = 20
TAKE_PREVIEW_WORDS = 40
ASSETS_PER_POST = 6


class PostFormat(StrEnum):
    REEL = "reel"
    CAROUSEL = "carousel"
    STORY = "story"


MEDIA_FOR_FORMAT = {PostFormat.REEL: "video", PostFormat.CAROUSEL: "photo", PostFormat.STORY: None}


class PlannedPost(BaseModel):
    day: DayName
    pillar: str = Field(description="One pillar name, copied exactly from the list.")
    format: PostFormat
    angle: str = Field(description="The idea of the post in one line.")
    asset_query: str = Field(description="Plain words describing the photo or video to use.")
    take: int | None = Field(
        default=None, description="For a reel built on one of her takes: its number. Else null."
    )
    caption: str
    slides: list[str] = Field(
        default_factory=list,
        description=f"Carousel only: {MIN_SLIDES} to {MAX_SLIDES} slide texts, first is the hook.",
    )
    story_frames: list[str] = Field(
        default_factory=list, description="Story only: 3 to 5 short frame texts."
    )


class WeekPlan(BaseModel):
    posts: list[PlannedPost]


@dataclass(frozen=True)
class TakeSummary:
    uuid: str
    opening: str


@dataclass(frozen=True)
class PlannedWeek:
    start: date
    plan: WeekPlan
    take_uuids: dict[int, str]
    assets: dict[int, list[str]]


AssetFinder = Callable[[PlannedPost], list[str]]

SYSTEM = (
    "You plan one week of social posts for one creator, in her voice. Spread the week "
    "across her pillars and mix reels, carousels and stories. Captions match her past "
    "captions in rhythm, vocabulary and punctuation. No hashtags unless she uses them."
)


def week_start(today: date) -> date:
    """The next Monday, or today if today is Monday."""
    return today + timedelta(days=(7 - today.weekday()) % 7)


def pillar_counts(memberships: dict[str, set[str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for albums in memberships.values():
        for album in albums:
            counts[album] = counts.get(album, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


def take_summaries(connection: sqlite3.Connection, uuids: Sequence[str]) -> list[TakeSummary]:
    summaries: list[TakeSummary] = []
    for uuid in uuids[:MAX_TAKES_OFFERED]:
        transcript = load_transcript(connection, uuid)
        if transcript is not None:
            opening = " ".join(transcript.text.split()[:TAKE_PREVIEW_WORDS])
            summaries.append(TakeSummary(uuid, opening))
    return summaries


def week_request(
    start: date, brand: BrandProfile, pillars: dict[str, int], takes: list[TakeSummary]
) -> Request:
    pillar_lines = "\n".join(f"- {name} ({count} examples)" for name, count in pillars.items())
    take_lines = "\n".join(
        f"{number}. {take.opening}" for number, take in enumerate(takes, start=1)
    )
    prompt = (
        f"{voice_brief(brand)}\n\n"
        f"Pillars:\n{pillar_lines}\n\n"
        f"Her recorded talking takes, numbered, opening words only:\n{take_lines or 'none'}\n\n"
        f"Plan the week starting Monday {start.isoformat()}: exactly one post per day, "
        f"{', '.join(DAYS)}. Use at least three pillars and all three formats. A reel should "
        "use one of her takes when one fits. Write the full caption for every post, slide "
        f"texts for carousels and frame texts for stories. End captions with "
        f"'Comment {brand.voice.comment_word}' when the post offers something."
    )
    return Request(subject=f"week-{start.isoformat()}", system=SYSTEM, prompt=prompt)


def validate_plan(plan: WeekPlan, pillars: Sequence[str], take_count: int) -> None:
    days = [post.day for post in plan.posts]
    if sorted(days, key=DAYS.index) != list(DAYS):
        raise ClaudeError("Claude's plan does not cover each day once")
    for post in plan.posts:
        if post.pillar not in pillars:
            raise ClaudeError(f"Claude used an unknown pillar on {post.day}")
        if post.take is not None and not 1 <= post.take <= take_count:
            raise ClaudeError(f"Claude picked take {post.take} on {post.day}, which is not listed")
        if post.format is PostFormat.CAROUSEL and not MIN_SLIDES <= len(post.slides) <= MAX_SLIDES:
            raise ClaudeError(f"Claude's carousel on {post.day} has {len(post.slides)} slides")


def ordered(plan: WeekPlan) -> WeekPlan:
    return WeekPlan(posts=sorted(plan.posts, key=lambda post: DAYS.index(post.day)))


def assign_assets(
    start: date, plan: WeekPlan, takes: list[TakeSummary], finder: AssetFinder
) -> PlannedWeek:
    """Reels on a take use that take. Every other post gets the top search hits."""
    take_uuids: dict[int, str] = {}
    assets: dict[int, list[str]] = {}
    for index, post in enumerate(plan.posts):
        if post.take is not None:
            take_uuids[index] = takes[post.take - 1].uuid
            assets[index] = [take_uuids[index]]
        else:
            assets[index] = finder(post)[:ASSETS_PER_POST]
    return PlannedWeek(start=start, plan=plan, take_uuids=take_uuids, assets=assets)


def save_week(connection: sqlite3.Connection, week: PlannedWeek) -> None:
    connection.execute(
        """
        INSERT INTO weeks (week_start, model, plan_json, assets_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(week_start) DO UPDATE SET model = excluded.model,
            plan_json = excluded.plan_json, assets_json = excluded.assets_json,
            created_at = excluded.created_at
        """,
        (
            week.start.isoformat(),
            MODEL,
            week.plan.model_dump_json(),
            json.dumps({"takes": week.take_uuids, "assets": week.assets}),
            datetime.now(UTC).isoformat(),
        ),
    )
    connection.commit()


def load_week(connection: sqlite3.Connection, start: date) -> PlannedWeek | None:
    row = connection.execute(
        "SELECT plan_json, assets_json FROM weeks WHERE week_start = ?", (start.isoformat(),)
    ).fetchone()
    if row is None:
        return None
    stored = json.loads(row[1])
    return PlannedWeek(
        start=start,
        plan=WeekPlan.model_validate_json(row[0]),
        take_uuids={int(key): value for key, value in stored["takes"].items()},
        assets={int(key): value for key, value in stored["assets"].items()},
    )


def latest_week(connection: sqlite3.Connection) -> PlannedWeek | None:
    row = connection.execute("SELECT MAX(week_start) FROM weeks").fetchone()
    return None if row[0] is None else load_week(connection, date.fromisoformat(row[0]))


def plan_week(
    connection: sqlite3.Connection,
    start: date,
    brand: BrandProfile,
    pillars: dict[str, int],
    takes: list[TakeSummary],
    client: StructuredClient,
    finder: AssetFinder,
) -> PlannedWeek:
    if not pillars:
        raise ClaudeError("no pillar albums found. Run `loupe scan` first.")
    plan = client.parse(week_request(start, brand, pillars, takes), WeekPlan)
    validate_plan(plan, list(pillars), len(takes))
    week = assign_assets(start, ordered(plan), takes, finder)
    save_week(connection, week)
    return week
