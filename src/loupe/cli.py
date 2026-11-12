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
