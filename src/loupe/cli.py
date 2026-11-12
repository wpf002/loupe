"""Loupe command line. Commands get added milestone by milestone (see CLAUDE.md)."""

import sqlite3
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from loupe import __version__
from loupe.brand import BrandProfile
from loupe.claude import StructuredClient
from loupe.config import ConfigError, PillarConfig, load_pillar_config
from loupe.doctor import CheckResult, run_doctor
from loupe.index import IndexTotals, album_counts, connect, totals
from loupe.settings import (
    Settings,
    SettingsError,
    current_macos_user,
    load_settings,
    owner_account_problem,
)
from loupe.sheets import Tile
from loupe.train import PillarModel
from loupe.transcribe import Transcript
from loupe.vectors import VectorStore

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()

LimitOption = Annotated[
    int | None, typer.Option("--limit", min=1, help="Process only the first N items.")
]


@app.callback()
def main() -> None:
    """Sort an Apple Photos library into content pillars."""


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


def _fail(message: str) -> typer.Exit:
    console.print(f"[red]{message}[/red]")
    return typer.Exit(code=1)


def _load_settings() -> Settings:
    try:
        return load_settings()
    except SettingsError as error:
        raise _fail(str(error)) from error


def _load_config() -> PillarConfig:
    try:
        return load_pillar_config()
    except ConfigError as error:
        raise _fail(str(error)) from error


def _require_owner_account(settings: Settings) -> None:
    problem = owner_account_problem(settings.owner_macos_user, current_macos_user())
    if problem is not None:
        raise _fail(f"Refusing to open a Photos library: {problem}. Run `loupe doctor`.")


def _doctor_table(results: list[CheckResult]) -> Table:
    table = Table(title="Loupe Doctor")
    for column in ("Check", "Status", "Detail", "Fix"):
        table.add_column(column)
    for result in results:
        status = "[green]OK[/green]" if result.ok else "[red]FAIL[/red]"
        table.add_row(result.name, status, result.detail, result.fix or "")
    return table


@app.command()
def doctor() -> None:
    """Check this account is ready to run Loupe."""
    results = run_doctor(_load_settings())
    console.print(_doctor_table(results))
    if not all(result.ok for result in results):
        raise typer.Exit(code=1)


def _totals_table(summary: IndexTotals) -> Table:
    table = Table(title="Index Totals")
    table.add_column("Metric")
    table.add_column("Count", justify="right")
    rows = [
        ("Items", summary.items),
        ("Eligible", summary.eligible),
        ("Photos", summary.photos),
        ("Videos", summary.videos),
        ("With Local Preview", summary.with_preview),
        ("Original Local", summary.originals_local),
        ("Screenshots", summary.screenshots),
        ("Screen Recordings", summary.screen_recordings),
        ("Bursts", summary.bursts),
    ]
    rows.extend(
        (f"Excluded: {reason}", count) for reason, count in summary.excluded_by_reason.items()
    )
    for metric, count in rows:
        table.add_row(metric, str(count))
    return table


def _albums_table(counts: dict[str, int], config: PillarConfig) -> Table:
    table = Table(title="Albums")
    table.add_column("Album")
    table.add_column("Items", justify="right")
    table.add_column("Pillar")
    for album, count in counts.items():
        if not config.is_pillar_album(album):
            pillar = ""
        elif count < config.min_examples_per_pillar:
            pillar = "too few"
        else:
            pillar = "yes"
        table.add_row(album, str(count), pillar)
    return table


@app.command()
def scan(limit: LimitOption = None) -> None:
    """M1: read the Photos library into the SQLite index."""
    from loupe.library import LibraryNotFoundError, iter_library_items, resolve_library_path
    from loupe.scan import scan_items

    settings = _load_settings()
    config = _load_config()
    _require_owner_account(settings)
    try:
        library_path = resolve_library_path(settings.photos_library)
    except LibraryNotFoundError as error:
        raise _fail(str(error)) from error
    connection = connect(settings.index_path)
    try:
        items = iter_library_items(library_path, limit)
        result = scan_items(connection, items, config.exclusions)
        console.print(f"Scanned {result.scanned} items, {result.excluded} excluded.")
        console.print(_totals_table(totals(connection)))
        console.print(_albums_table(album_counts(connection), config))
    finally:
        connection.close()


@app.command()
def embed(limit: LimitOption = None) -> None:
    """M2: embed every eligible preview with SigLIP on MPS. Safe to re-run."""
    from rich.progress import Progress

    from loupe.embed import embed_items, pending_work, vector_path
    from loupe.encoder import SiglipEncoder
    from loupe.vectors import VectorStore, VectorStoreError

    settings = _load_settings()
    _require_owner_account(settings)
    connection = connect(settings.index_path)
    try:
        console.print("Loading SigLIP model...")
        encoder = SiglipEncoder()
        try:
            store = VectorStore(vector_path(settings.data_dir, encoder.model_id), encoder.dim)
        except VectorStoreError as error:
            raise _fail(str(error)) from error
        total = len(pending_work(connection, encoder.model_id, limit))
        with Progress(console=console) as progress:
            task = progress.add_task("Embedding", total=total)
            result = embed_items(
                connection,
                store,
                encoder,
                limit,
                on_batch=lambda count: progress.advance(task, count),
            )
        table = Table("Result", "Count")
        table.add_row("Embedded", str(result.embedded))
        for reason, count in sorted(result.skipped.items()):
            table.add_row(f"Skipped: {reason}", str(count))
        table.add_row("Purged (now excluded)", str(result.purged))
        console.print(table)
        console.print(f"{result.seconds:.1f}s, {result.per_second:.1f} items/sec")
    finally:
        connection.close()


def _open_vectors(settings: Settings, model_id: str) -> VectorStore:
    from loupe.embed import vector_path
    from loupe.vectors import VectorStoreError

    try:
        return VectorStore(vector_path(settings.data_dir, model_id))
    except VectorStoreError as error:
        raise _fail(str(error)) from error


@app.command()
def train(limit: LimitOption = None) -> None:
    """M3: fit the pillar classifier on her labeled albums and score a held-out split."""
    from loupe.encoder import MODEL_ID
    from loupe.train import TrainingError, load_training_set, model_path, save_model, train_pillars

    settings = _load_settings()
    config = _load_config()
    _require_owner_account(settings)
    connection = connect(settings.index_path)
    try:
        store = _open_vectors(settings, MODEL_ID)
        training_set = load_training_set(connection, store, config, MODEL_ID, limit)
        try:
            result = train_pillars(training_set, config, MODEL_ID)
        except TrainingError as error:
            raise _fail(str(error)) from error
        save_model(result.model, model_path(settings.data_dir))
    finally:
        connection.close()
    table = Table("Pillar", "Train", "Held Out", "Precision", "Recall")
    for score in result.scores:
        table.add_row(
            score.pillar,
            str(score.train_examples),
            str(score.test_examples),
            f"{score.precision:.2f}",
            f"{score.recall:.2f}",
        )
    console.print(table)
    console.print(
        f"Scored at min_confidence {config.min_confidence:.2f}. "
        f"{result.background} unlabeled items used as background."
    )
    if result.too_small:
        small = Table("Too Few Examples", "Count")
        for pillar, count in result.too_small.items():
            small.add_row(pillar, str(count))
        console.print(small)
        console.print(f"Need {config.min_examples_per_pillar} per pillar.")


def _load_model(settings: Settings) -> PillarModel:
    from loupe.train import TrainingError, load_model, model_path

    try:
        return load_model(model_path(settings.data_dir))
    except TrainingError as error:
        raise _fail(str(error)) from error


@app.command()
def classify(limit: LimitOption = None) -> None:
    """M4: score every item outside her pillar albums."""
    from loupe.classify import classify_items

    settings = _load_settings()
    config = _load_config()
    _require_owner_account(settings)
    model = _load_model(settings)
    connection = connect(settings.index_path)
    try:
        store = _open_vectors(settings, model.model_id)
        result = classify_items(connection, store, model, config, limit)
    finally:
        connection.close()
    table = Table("Pillar", "Confident", "Borderline")
    for pillar in model.pillars:
        table.add_row(pillar, str(result.confident[pillar]), str(result.borderline[pillar]))
    console.print(table)
    console.print(f"Scored {result.scored} items, purged {result.purged} stale scores.")


@app.command()
def review(
    open_page: Annotated[bool, typer.Option("--open", help="Open the index page.")] = False,
) -> None:
    """M4: write a contact sheet per pillar to data/review/."""
    import webbrowser

    from loupe.review import write_review

    settings = _load_settings()
    config = _load_config()
    _require_owner_account(settings)
    model = _load_model(settings)
    connection = connect(settings.index_path)
    try:
        pages = write_review(connection, model.pillars, config, settings.data_dir / "review")
    finally:
        connection.close()
    console.print(f"Wrote {len(pages.pillars)} pillar sheets. Index: {pages.index}")
    if open_page:
        webbrowser.open(pages.index.resolve().as_uri())


@app.command("write-albums")
def write_albums(
    limit: LimitOption = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Write to Photos. Default is a dry run.")
    ] = False,
) -> None:
    """M5: add confident picks to albums in the Loupe folder. Dry run unless --apply."""
    from loupe.albums import PhotoscriptWriter, apply_plans, plan_albums

    settings = _load_settings()
    config = _load_config()
    _require_owner_account(settings)
    model = _load_model(settings)
    connection = connect(settings.index_path)
    try:
        plans = plan_albums(connection, model.pillars, config, limit)
        table = Table("Album", "Pillar", "Confident", "Already Added", "To Add")
        for plan in plans:
            table.add_row(
                f"{settings.album_folder} / {plan.album}",
                plan.pillar,
                str(plan.picks),
                str(plan.already_written),
                str(len(plan.to_add)),
            )
        console.print(table)
        if not apply:
            console.print("Dry run. Nothing was written. Pass --apply to write.")
            return
        writer = PhotoscriptWriter(settings.album_folder)
        added = 0
        for album, count in apply_plans(connection, plans, writer):
            added += count
            console.print(f"{album}: +{count}")
        console.print(f"Added {added} items.")
    finally:
        connection.close()


@app.command("search")
def search_command(
    query: Annotated[str, typer.Argument(help="What to look for, in plain words.")],
    limit: Annotated[int, typer.Option("--limit", min=1, help="Results to return.")] = 60,
    pillar: Annotated[str | None, typer.Option(help="Pillar album name.")] = None,
    media: Annotated[str | None, typer.Option(help="photo or video.")] = None,
    person: Annotated[str | None, typer.Option(help="Named person from Photos.")] = None,
    light: Annotated[str | None, typer.Option(help="natural or artificial.")] = None,
    shot: Annotated[str | None, typer.Option(help="close-up, medium, wide or selfie.")] = None,
    setting: Annotated[str | None, typer.Option(help="indoor or outdoor.")] = None,
    open_page: Annotated[bool, typer.Option("--open", help="Open the contact sheet.")] = False,
) -> None:
    """M6: search the image bank by text and open a contact sheet."""
    import webbrowser

    from loupe.encoder import SiglipEncoder
    from loupe.search import Facet, SearchError, SearchFilters, search
    from loupe.sheets import Section, Tile, slug, write_sheet

    settings = _load_settings()
    config = _load_config()
    _require_owner_account(settings)
    chosen = {Facet.LIGHT: light, Facet.SHOT: shot, Facet.SETTING: setting}
    filters = SearchFilters(
        pillar=pillar,
        media_type=media,
        person=person,
        facets={facet: value for facet, value in chosen.items() if value is not None},
    )
    encoder = SiglipEncoder()
    connection = connect(settings.index_path)
    try:
        store = _open_vectors(settings, encoder.model_id)
        try:
            hits = search(connection, store, encoder, query, filters, config, limit)
        except SearchError as error:
            raise _fail(str(error)) from error
    finally:
        connection.close()
    for hit in hits:
        typer.echo(hit.uuid)
    search_dir = settings.data_dir / "search"
    tiles = [Tile(hit.uuid, hit.preview_path, hit.media_type) for hit in hits]
    sheet = write_sheet(
        search_dir / f"{slug(query)}.html",
        query or "All",
        [Section("Results", tiles, len(tiles))],
        settings.data_dir / "review" / "thumbs",
        {"Results": len(hits)},
    )
    console.print(f"{len(hits)} results. Sheet: {sheet}")
    if open_page:
        webbrowser.open(sheet.resolve().as_uri())


def _load_brand() -> BrandProfile:
    from loupe.brand import BrandError, load_brand

    try:
        return load_brand()
    except BrandError as error:
        raise _fail(str(error)) from error


@app.command()
def brand(
    open_page: Annotated[bool, typer.Option("--open", help="Open the preview page.")] = False,
) -> None:
    """M7: check config/brand.toml and write a preview page."""
    import webbrowser

    from loupe.brand import brand_preview_html

    settings = _load_settings()
    profile = _load_brand()
    table = Table("Brand", "Value")
    table.add_row("Colours", ", ".join(profile.colors.model_dump().values()))
    table.add_row("Display Font", profile.fonts.display.name)
    table.add_row("Body Font", profile.fonts.body.name)
    table.add_row("Voice Samples", str(len(profile.voice.samples)))
    table.add_row("Comment Word", profile.voice.comment_word)
    console.print(table)
    preview = settings.data_dir / "brand" / "index.html"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_text(brand_preview_html(profile))
    console.print(f"Preview: {preview}")
    if open_page:
        webbrowser.open(preview.resolve().as_uri())


@app.command()
def transcribe(limit: LimitOption = None) -> None:
    """M8: transcribe local video originals with word timestamps. Safe to re-run."""
    from rich.progress import Progress

    from loupe.library import LibraryNotFoundError, original_paths, resolve_library_path
    from loupe.transcribe import pending_clips, transcribe_clips

    settings = _load_settings()
    _require_owner_account(settings)
    try:
        library_path = resolve_library_path(settings.photos_library)
    except LibraryNotFoundError as error:
        raise _fail(str(error)) from error
    try:
        from loupe.transcribe import MlxTranscriber

        transcriber = MlxTranscriber()
    except ImportError as error:
        raise _fail("mlx-whisper is not installed. Run `uv sync --extra transcribe`.") from error
    connection = connect(settings.index_path)
    try:
        pending = pending_clips(connection, limit)
        paths = original_paths(library_path, pending)
        with Progress(console=console) as progress:
            task = progress.add_task("Transcribing", total=len(pending))
            result = transcribe_clips(
                connection, pending, paths, transcriber, on_item=lambda: progress.advance(task)
            )
    finally:
        connection.close()
    console.print(f"Transcribed {result.transcribed} clips, {result.talking} with talking.")
    for reason, count in result.skipped.items():
        console.print(f"Skipped {count}: {reason}")


def _original_path(settings: Settings, uuid: str) -> Path:
    from loupe.library import LibraryNotFoundError, original_paths, resolve_library_path

    _require_owner_account(settings)
    try:
        library_path = resolve_library_path(settings.photos_library)
    except LibraryNotFoundError as error:
        raise _fail(str(error)) from error
    found = original_paths(library_path, [uuid]).get(uuid)
    if found is None:
        raise _fail(f"original for {uuid} is not on this Mac")
    return found


def _take(connection: sqlite3.Connection, uuid: str) -> tuple[Transcript, float]:
    from loupe.transcribe import load_transcript, talking_clips

    if uuid not in talking_clips(connection):
        raise _fail(f"{uuid} is not a transcribed talking clip. Run `loupe cut` to list them.")
    transcript = load_transcript(connection, uuid)
    row = connection.execute(
        "SELECT duration_seconds FROM items WHERE uuid = ?", (uuid,)
    ).fetchone()
    if transcript is None or row is None or row[0] is None:
        raise _fail(f"{uuid} has no transcript or duration")
    return transcript, float(row[0])


@app.command()
def cut(
    uuid: Annotated[str | None, typer.Argument(help="Take to cut. Omit to list takes.")] = None,
    limit: LimitOption = None,
) -> None:
    """M9: remove dead air and filler words from a take. Writes to data/renders/."""
    from loupe.audio import AudioError
    from loupe.cut import cut_path, plan_cut, render_cut
    from loupe.transcribe import talking_clips

    settings = _load_settings()
    connection = connect(settings.index_path)
    try:
        if uuid is None:
            table = Table("Take", "Seconds", "Words")
            for take in talking_clips(connection)[:limit]:
                seconds, words = connection.execute(
                    "SELECT items.duration_seconds, transcripts.word_count FROM items "
                    "JOIN transcripts ON transcripts.uuid = items.uuid WHERE items.uuid = ?",
                    (take,),
                ).fetchone()
                table.add_row(take, f"{seconds:.1f}", str(words))
            console.print(table)
            return
        transcript, seconds = _take(connection, uuid)
    finally:
        connection.close()
    plan = plan_cut(transcript, seconds)
    target = cut_path(settings.data_dir / "renders", uuid)
    try:
        render_cut(_original_path(settings, uuid), target, plan)
    except (AudioError, ValueError) as error:
        raise _fail(f"cut failed for {uuid}: {error}") from error
    console.print(
        f"Kept {plan.kept_seconds:.1f}s of {plan.source_seconds:.1f}s in {len(plan.ranges)} "
        f"ranges, {plan.fillers_removed} fillers removed."
    )
    console.print(f"Render: {target}")


def _claude_client() -> StructuredClient:
    from loupe.claude import AnthropicClient, ClaudeError

    try:
        return AnthropicClient()
    except ImportError as error:
        raise _fail("anthropic is not installed. Run `uv sync --extra secondpass`.") from error
    except ClaudeError as error:
        raise _fail(str(error)) from error


@app.command()
def hooks(
    uuid: Annotated[str, typer.Argument(help="Talking take, from `loupe cut`.")],
    fresh: Annotated[bool, typer.Option("--fresh", help="Ask Claude for new hooks.")] = False,
) -> None:
    """M10: six hook openings for one take, in her voice. Sends the transcript to Claude."""
    from loupe.claude import ClaudeError
    from loupe.hooks import hooks_for_take

    settings = _load_settings()
    profile = _load_brand()
    connection = connect(settings.index_path)
    try:
        transcript, _ = _take(connection, uuid)
        try:
            lines = hooks_for_take(
                connection, uuid, profile, transcript, _claude_client(), fresh=fresh
            )
        except ClaudeError as error:
            raise _fail(str(error)) from error
    finally:
        connection.close()
    table = Table("#", "Hook")
    for number, line in enumerate(lines, start=1):
        table.add_row(str(number), line)
    console.print(table)
    console.print(f"Render one with `loupe reel {uuid} --hook N`.")


@app.command()
def reel(
    uuid: Annotated[str, typer.Argument(help="Take with hooks, from `loupe hooks`.")],
    hook: Annotated[int, typer.Option("--hook", min=1, max=6, help="Hook number.")] = 1,
    length: Annotated[
        float, typer.Option("--length", min=5.0, max=90.0, help="Seconds before the end card.")
    ] = 30.0,
) -> None:
    """M10: render the cut take as a 9:16 reel with captions and an end card."""
    from loupe.audio import AudioError
    from loupe.cut import cut_path, plan_cut, remap_words, render_cut
    from loupe.hooks import load_hooks
    from loupe.reel import ReelSpec, reel_path, render_reel

    settings = _load_settings()
    profile = _load_brand()
    connection = connect(settings.index_path)
    try:
        transcript, seconds = _take(connection, uuid)
        stored = load_hooks(connection, uuid)
    finally:
        connection.close()
    if stored is None:
        raise _fail(f"no hooks for {uuid}. Run `loupe hooks {uuid}` first.")
    plan = plan_cut(transcript, seconds)
    renders = settings.data_dir / "renders"
    cut_video = cut_path(renders, uuid)
    spec = ReelSpec(
        hook=stored[hook - 1],
        body_seconds=min(length, plan.kept_seconds),
        comment_word=profile.voice.comment_word,
    )
    target = reel_path(renders, uuid, hook)
    try:
        if not cut_video.is_file():
            render_cut(_original_path(settings, uuid), cut_video, plan)
        render_reel(cut_video, target, profile, spec, remap_words(transcript, plan))
    except (AudioError, ValueError) as error:
        raise _fail(f"reel failed for {uuid}: {error}") from error
    console.print(f"Reel {spec.total_seconds:.1f}s with hook {hook}: {target}")


def _week_tiles(connection: sqlite3.Connection, uuids: list[str]) -> list[Tile]:
    tiles = []
    for uuid in uuids:
        row = connection.execute(
            "SELECT preview_path, media_type FROM items WHERE uuid = ?", (uuid,)
        ).fetchone()
        if row is not None:
            tiles.append(Tile(uuid, row[0], row[1]))
    return tiles


def _monday(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise _fail("--start must be YYYY-MM-DD") from error
    if parsed.weekday() != 0:
        raise _fail("--start must be a Monday")
    return parsed


@app.command()
def week(
    start: Annotated[
        str | None, typer.Option("--start", help="Monday to plan, YYYY-MM-DD. Default: next.")
    ] = None,
    fresh: Annotated[bool, typer.Option("--fresh", help="Ask Claude for a new plan.")] = False,
    open_page: Annotated[bool, typer.Option("--open", help="Open the week sheet.")] = False,
) -> None:
    """M11: plan Monday to Sunday with Claude and pick assets through search."""
    import webbrowser

    from loupe.claude import ClaudeError
    from loupe.encoder import SiglipEncoder
    from loupe.search import SearchError, SearchFilters, search
    from loupe.sheets import Section, write_sheet
    from loupe.train import pillar_members
    from loupe.transcribe import talking_clips
    from loupe.week import (
        MEDIA_FOR_FORMAT,
        PlannedPost,
        load_week,
        pillar_counts,
        plan_week,
        take_summaries,
        week_start,
    )

    monday = _monday(start) if start else week_start(date.today())
    settings = _load_settings()
    config = _load_config()
    profile = _load_brand()
    _require_owner_account(settings)
    connection = connect(settings.index_path)
    try:
        planned = None if fresh else load_week(connection, monday)
        if planned is None:
            encoder = SiglipEncoder()
            store = _open_vectors(settings, encoder.model_id)

            def find_assets(post: PlannedPost) -> list[str]:
                filters = SearchFilters(
                    pillar=post.pillar, media_type=MEDIA_FOR_FORMAT[post.format]
                )
                hits = search(connection, store, encoder, post.asset_query, filters, config)
                return [hit.uuid for hit in hits]

            try:
                planned = plan_week(
                    connection,
                    monday,
                    profile,
                    pillar_counts(pillar_members(connection, config)),
                    take_summaries(connection, talking_clips(connection)),
                    _claude_client(),
                    find_assets,
                )
            except (ClaudeError, SearchError) as error:
                raise _fail(str(error)) from error
        sections = [
            Section(
                f"{post.day} · {post.format.value.title()} · {post.pillar}",
                _week_tiles(connection, planned.assets.get(index, [])),
                len(planned.assets.get(index, [])),
            )
            for index, post in enumerate(planned.plan.posts)
        ]
    finally:
        connection.close()
    table = Table("Day", "Format", "Pillar", "Angle", "Assets")
    for index, post in enumerate(planned.plan.posts):
        table.add_row(
            post.day, post.format.value, post.pillar, post.angle,
            str(len(planned.assets.get(index, []))),
        )  # fmt: skip
    console.print(table)
    sheet = write_sheet(
        settings.data_dir / "week" / f"{monday.isoformat()}.html",
        f"Week of {monday.strftime('%b %-d')}",
        sections,
        settings.data_dir / "review" / "thumbs",
    )
    console.print(f"Sheet: {sheet}")
    if open_page:
        webbrowser.open(sheet.resolve().as_uri())


@app.command()
def carousel(
    start: Annotated[
        str | None, typer.Option("--start", help="Planned Monday, YYYY-MM-DD. Default: latest.")
    ] = None,
    day: Annotated[str | None, typer.Option("--day", help="Mon to Sun. Default: all.")] = None,
) -> None:
    """M12: render the week's carousels as slide images. Writes to data/renders/."""
    from loupe.carousel import carousel_dir, render_carousel
    from loupe.week import PostFormat, latest_week, load_week

    settings = _load_settings()
    profile = _load_brand()
    connection = connect(settings.index_path)
    try:
        planned = load_week(connection, _monday(start)) if start else latest_week(connection)
        if planned is None:
            raise _fail("no week planned. Run `loupe week` first.")
        jobs = []
        for index, post in enumerate(planned.plan.posts):
            if post.format is not PostFormat.CAROUSEL or (day and post.day != day.title()):
                continue
            assets = planned.assets.get(index, [])
            row = (
                connection.execute(
                    "SELECT preview_path FROM items WHERE uuid = ?", (assets[0],)
                ).fetchone()
                if assets
                else None
            )
            jobs.append((post, Path(row[0]) if row and row[0] else None))
    finally:
        connection.close()
    if not jobs:
        raise _fail("no carousel in that week or on that day")
    renders = settings.data_dir / "renders"
    for post, preview in jobs:
        target = carousel_dir(renders, planned.start.isoformat(), post.day)
        paths = render_carousel(profile, post.slides, preview, target)
        console.print(f"{post.day}: {len(paths)} slides in {target}")
