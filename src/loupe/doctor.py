"""Preflight checks for the macOS account that holds the owner's library."""

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from loupe.config import ConfigError, load_pillar_config
from loupe.settings import Settings, current_macos_user, owner_account_problem

BYTES_PER_GB = 1024**3
MIN_FREE_DISK_GB = 20
# Readable only with Full Disk Access. The per-user copy is absent on some macOS versions.
TCC_DATABASES = (
    Path.home() / "Library/Application Support/com.apple.TCC/TCC.db",
    Path("/Library/Application Support/com.apple.TCC/TCC.db"),
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    fix: str | None = None


def check_owner_account(settings: Settings, current_user: str) -> CheckResult:
    problem = owner_account_problem(settings.owner_macos_user, current_user)
    if problem is None:
        return CheckResult("Owner Account", True, current_user)
    return CheckResult(
        "Owner Account",
        False,
        problem,
        "Run Loupe only in the owner's macOS account, and set LOUPE_OWNER_MACOS_USER "
        "in .env to that account's short name (run `whoami` there to get it).",
    )


def check_full_disk_access(probe_paths: tuple[Path, ...] = TCC_DATABASES) -> CheckResult:
    """Open the first TCC database that exists. Opened, never read."""
    probe = next((path for path in probe_paths if path.exists()), None)
    if probe is None:
        return CheckResult(
            "Full Disk Access",
            False,
            "could not verify, no TCC database found",
            "Grant Full Disk Access to your terminal app in System Settings and rerun.",
        )
    try:
        with probe.open("rb"):
            pass
    except PermissionError:
        return CheckResult(
            "Full Disk Access",
            False,
            "denied",
            "System Settings > Privacy & Security > Full Disk Access: turn on your "
            "terminal app, then quit and reopen it.",
        )
    return CheckResult("Full Disk Access", True, "granted")


def check_config() -> CheckResult:
    try:
        load_pillar_config()
    except ConfigError as error:
        return CheckResult("Pillar Config", False, str(error), "Fix config/pillars.toml.")
    return CheckResult("Pillar Config", True, "valid")


def check_mps(is_available: Callable[[], bool] | None = None) -> CheckResult:
    if is_available is None:
        import torch

        is_available = torch.backends.mps.is_available
    if is_available():
        return CheckResult("MPS", True, "available")
    return CheckResult(
        "MPS",
        False,
        "not available",
        "Use an Apple Silicon Mac on a current macOS, and run `uv sync` to reinstall torch.",
    )


def _existing_ancestor(path: Path) -> Path:
    candidate = path.resolve()
    while not candidate.exists():
        candidate = candidate.parent
    return candidate


def check_free_disk(data_dir: Path, minimum_gb: int = MIN_FREE_DISK_GB) -> CheckResult:
    free_gb = shutil.disk_usage(_existing_ancestor(data_dir)).free / BYTES_PER_GB
    detail = f"{free_gb:.0f} GB free at {data_dir}"
    if free_gb >= minimum_gb:
        return CheckResult("Free Disk", True, detail)
    return CheckResult(
        "Free Disk",
        False,
        detail,
        f"Free up space until at least {minimum_gb} GB is available.",
    )


def check_library(settings: Settings) -> list[CheckResult]:
    # Imported here so the account and permission checks run even if osxphotos is broken.
    from loupe.library import LibraryNotFoundError, library_stats, resolve_library_path

    try:
        library_path = resolve_library_path(settings.photos_library)
    except LibraryNotFoundError as error:
        return [
            CheckResult(
                "Library Path",
                False,
                str(error),
                "Open Photos once in this account and let iCloud finish syncing, "
                "or set LOUPE_PHOTOS_LIBRARY in .env to the .photoslibrary path.",
            )
        ]
    stats = library_stats(library_path)
    return [
        CheckResult("Library Path", True, str(library_path)),
        CheckResult(
            "Total Items",
            stats.total > 0,
            str(stats.total),
            None if stats.total else "Photos shows no items. Wait for iCloud sync.",
        ),
        CheckResult("Local Previews", True, f"{stats.with_preview} of {stats.total}"),
        CheckResult("Local Originals", True, f"{stats.originals_local} of {stats.total}"),
    ]


def run_doctor(settings: Settings) -> list[CheckResult]:
    account = check_owner_account(settings, current_macos_user())
    disk_access = check_full_disk_access()
    results = [
        account,
        disk_access,
        check_config(),
        check_mps(),
        check_free_disk(settings.data_dir),
    ]
    if account.ok and disk_access.ok:
        results.extend(check_library(settings))
    else:
        results.append(
            CheckResult(
                "Library",
                False,
                "not opened",
                "Fix Owner Account and Full Disk Access first.",
            )
        )
    return results
