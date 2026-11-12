"""Loupe command line. Commands get added milestone by milestone (see CLAUDE.md)."""

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from loupe import __version__
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
from loupe.train import PillarModel
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
