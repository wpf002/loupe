"""M14 service layer: the hooks, reel, week and everything flows shared by the CLI and the app.

Every function raises StudioError with a message fit to show the owner. Nothing here
opens the Photos library except original_path, which checks the owner account first.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from loupe.brand import BrandProfile
from loupe.claude import ClaudeError, ProxyAuth, StructuredClient
from loupe.config import PillarConfig
from loupe.settings import Settings, current_macos_user, owner_account_problem
from loupe.transcribe import Transcript
from loupe.week import PlannedPost, PlannedWeek

OPENING_WORDS = 14
HOOK_NUMBERS = range(1, 7)

WeekPlanner = Callable[[sqlite3.Connection, date], PlannedWeek]


class StudioError(Exception):
    """A failure the owner can act on. The message is safe to display."""


@dataclass(frozen=True)
class Studio:
    """Everything the local app needs, built once by `loupe app` after the owner check."""

    settings: Settings
    brand: BrandProfile
    client_factory: Callable[[], StructuredClient]
    planner: WeekPlanner
    find_original: Callable[[str], Path]


@dataclass(frozen=True)
class TakeRow:
    uuid: str
    seconds: float
    words: int
    opening: str
    has_hooks: bool


@dataclass(frozen=True)
class RenderedSet:
    day: str
    format: str
    files: list[Path]
    folder: Path


def renders_dir(settings: Settings) -> Path:
    return settings.data_dir / "renders"


def take(connection: sqlite3.Connection, uuid: str) -> tuple[Transcript, float]:
    from loupe.transcribe import load_transcript, talking_clips

    if uuid not in talking_clips(connection):
        raise StudioError(f"{uuid} is not a transcribed talking clip. Run `loupe cut` to list.")
    transcript = load_transcript(connection, uuid)
    row = connection.execute(
        "SELECT duration_seconds FROM items WHERE uuid = ?", (uuid,)
    ).fetchone()
    if transcript is None or row is None or row[0] is None:
        raise StudioError(f"{uuid} has no transcript or duration")
    return transcript, float(row[0])


def take_list(connection: sqlite3.Connection) -> list[TakeRow]:
    from loupe.hooks import load_hooks
    from loupe.transcribe import load_transcript, talking_clips

    rows: list[TakeRow] = []
    for uuid in talking_clips(connection):
        transcript = load_transcript(connection, uuid)
        seconds = connection.execute(
            "SELECT duration_seconds FROM items WHERE uuid = ?", (uuid,)
        ).fetchone()[0]
        if transcript is None or seconds is None:
            continue
        words = transcript.text.split()
        opening = " ".join(words[:OPENING_WORDS]) + (" ..." if len(words) > OPENING_WORDS else "")
        has_hooks = load_hooks(connection, uuid) is not None
        rows.append(TakeRow(uuid, float(seconds), len(words), opening, has_hooks))
    return rows


def original_path(settings: Settings, uuid: str) -> Path:
    from loupe.library import LibraryNotFoundError, original_paths, resolve_library_path

    problem = owner_account_problem(settings.owner_macos_user, current_macos_user())
    if problem is not None:
        raise StudioError(f"Refusing to open a Photos library: {problem}. Run `loupe doctor`.")
    try:
        library_path = resolve_library_path(settings.photos_library)
    except LibraryNotFoundError as error:
        raise StudioError(str(error)) from error
    found = original_paths(library_path, [uuid]).get(uuid)
    if found is None:
        raise StudioError(f"original for {uuid} is not on this Mac")
    return found


def claude_client(proxy: ProxyAuth | None = None) -> StructuredClient:
    """The CLI calls Claude with its own key; Loupe.app passes a proxy to the Loupe server."""
    from loupe.claude import AnthropicClient

    try:
        return AnthropicClient(proxy)
    except ImportError as error:
        message = "anthropic is not installed. Run `uv sync --extra secondpass`."
        raise StudioError(message) from error
    except ClaudeError as error:
        raise StudioError(str(error)) from error


def write_hooks(
    connection: sqlite3.Connection,
    uuid: str,
    brand: BrandProfile,
    client_factory: Callable[[], StructuredClient],
    fresh: bool = False,
) -> tuple[str, ...]:
    from loupe.hooks import hooks_for_take, load_hooks

    transcript, _ = take(connection, uuid)
    if not fresh:
        stored = load_hooks(connection, uuid)
        if stored is not None:
            return stored
    try:
        return hooks_for_take(connection, uuid, brand, transcript, client_factory(), fresh=fresh)
    except ClaudeError as error:
        raise StudioError(str(error)) from error


def rendered_reels(settings: Settings, uuid: str) -> list[int]:
    from loupe.reel import reel_path

    return [
        number
        for number in HOOK_NUMBERS
        if reel_path(renders_dir(settings), uuid, number).is_file()
    ]


def make_reel(
    settings: Settings,
    connection: sqlite3.Connection,
    brand: BrandProfile,
    uuid: str,
    hook: int,
    length: float,
    find_original: Callable[[str], Path],
) -> tuple[Path, float]:
    """Render the cut take with hook number `hook`. Returns the file and its seconds."""
    from loupe.audio import AudioError
    from loupe.cut import cut_path, plan_cut, remap_words, render_cut
    from loupe.hooks import load_hooks
    from loupe.reel import ReelSpec, reel_path, render_reel

    if hook not in HOOK_NUMBERS:
        raise StudioError("hook must be 1 to 6")
    transcript, seconds = take(connection, uuid)
    stored = load_hooks(connection, uuid)
    if stored is None:
        raise StudioError(f"no hooks for {uuid}. Write hooks first.")
    plan = plan_cut(transcript, seconds)
    cut_video = cut_path(renders_dir(settings), uuid)
    spec = ReelSpec(
        hook=stored[hook - 1],
        body_seconds=min(length, plan.kept_seconds),
        comment_word=brand.voice.comment_word,
    )
    target = reel_path(renders_dir(settings), uuid, hook)
    try:
        if not cut_video.is_file():
            render_cut(find_original(uuid), cut_video, plan)
        render_reel(cut_video, target, brand, spec, remap_words(transcript, plan))
    except (AudioError, ValueError) as error:
        raise StudioError(f"reel failed for {uuid}: {error}") from error
    return target, spec.total_seconds


def search_planner(
    settings: Settings,
    config: PillarConfig,
    brand: BrandProfile,
    client_factory: Callable[[], StructuredClient],
) -> WeekPlanner:
    """Plans a week with Claude, picking assets through M6 search on the M2 vectors."""

    def plan(connection: sqlite3.Connection, monday: date) -> PlannedWeek:
        from loupe.embed import vector_path
        from loupe.encoder import SiglipEncoder
        from loupe.search import SearchError, SearchFilters, search
        from loupe.train import pillar_members
        from loupe.transcribe import talking_clips
        from loupe.vectors import VectorStore, VectorStoreError
        from loupe.week import MEDIA_FOR_FORMAT, pillar_counts, plan_week, take_summaries

        encoder = SiglipEncoder()
        try:
            store = VectorStore(vector_path(settings.data_dir, encoder.model_id))
        except VectorStoreError as error:
            raise StudioError(str(error)) from error

        def find_assets(post: PlannedPost) -> list[str]:
            filters = SearchFilters(pillar=post.pillar, media_type=MEDIA_FOR_FORMAT[post.format])
            hits = search(connection, store, encoder, post.asset_query, filters, config)
            return [hit.uuid for hit in hits]

        try:
            return plan_week(
                connection,
                monday,
                brand,
                pillar_counts(pillar_members(connection, config)),
                take_summaries(connection, talking_clips(connection)),
                client_factory(),
                find_assets,
            )
        except (ClaudeError, SearchError) as error:
            raise StudioError(str(error)) from error

    return plan


def plan_or_load_week(
    connection: sqlite3.Connection, monday: date, planner: WeekPlanner, fresh: bool = False
) -> PlannedWeek:
    from loupe.week import load_week

    planned = None if fresh else load_week(connection, monday)
    return planned if planned is not None else planner(connection, monday)


def parse_monday(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise StudioError("start must be YYYY-MM-DD") from error
    if parsed.weekday() != 0:
        raise StudioError("start must be a Monday")
    return parsed


def planned_week(connection: sqlite3.Connection, start: str | None) -> PlannedWeek:
    from loupe.week import latest_week, load_week

    planned = load_week(connection, parse_monday(start)) if start else latest_week(connection)
    if planned is None:
        raise StudioError("no week planned. Run `loupe week` first.")
    return planned


def covers_dir(settings: Settings) -> Path:
    return settings.data_dir / "covers"


def cover_preview(
    connection: sqlite3.Connection, planned: PlannedWeek, index: int, cache_dir: Path
) -> Path | None:
    from loupe.previews import preview_file

    assets = planned.assets.get(index, [])
    if not assets:
        return None
    row = connection.execute(
        "SELECT preview_path FROM items WHERE uuid = ?", (assets[0],)
    ).fetchone()
    return preview_file(row[0], cache_dir) if row else None


def posts_with_covers(
    connection: sqlite3.Connection,
    planned: PlannedWeek,
    post_format: str,
    day: str | None,
    cache_dir: Path,
) -> list[tuple[PlannedPost, Path | None]]:
    return [
        (post, cover_preview(connection, planned, index, cache_dir))
        for index, post in enumerate(planned.plan.posts)
        if post.format == post_format and (day is None or post.day == day.title())
    ]


def email_path(settings: Settings, planned: PlannedWeek) -> Path:
    return renders_dir(settings) / "email" / f"{planned.start.isoformat()}.txt"


def build_everything(
    settings: Settings,
    connection: sqlite3.Connection,
    brand: BrandProfile,
    planned: PlannedWeek,
    client_factory: Callable[[], StructuredClient],
    fresh: bool = False,
) -> list[RenderedSet]:
    """Carousels, story frames and the email draft for one planned week."""
    from loupe.carousel import carousel_dir, render_carousel
    from loupe.newsletter import email_for_week, email_text
    from loupe.story import render_story, story_dir

    carousels = posts_with_covers(connection, planned, "carousel", None, covers_dir(settings))
    stories = posts_with_covers(connection, planned, "story", None, covers_dir(settings))
    try:
        draft = email_for_week(connection, planned, brand, client_factory(), fresh)
    except ClaudeError as error:
        raise StudioError(str(error)) from error
    week_key = planned.start.isoformat()
    renders = renders_dir(settings)
    built: list[RenderedSet] = []
    for post, preview in carousels:
        folder = carousel_dir(renders, week_key, post.day)
        built.append(
            RenderedSet(
                post.day, "carousel", render_carousel(brand, post.slides, preview, folder), folder
            )
        )
    for post, preview in stories:
        if not post.story_frames:
            continue
        folder = story_dir(renders, week_key, post.day)
        built.append(
            RenderedSet(
                post.day, "story", render_story(brand, post.story_frames, preview, folder), folder
            )
        )
    target = email_path(settings, planned)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(email_text(draft), encoding="utf-8")
    built.append(RenderedSet("Mon", "email", [target], target.parent))
    return built
