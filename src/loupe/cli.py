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
