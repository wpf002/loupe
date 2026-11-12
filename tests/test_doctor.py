from pathlib import Path

from loupe.doctor import check_free_disk, check_full_disk_access, check_mps, check_owner_account
from loupe.settings import Settings, owner_account_problem


def _settings(owner: str | None) -> Settings:
    return Settings(
        data_dir=Path("data"), photos_library=None, album_folder="Loupe", owner_macos_user=owner
    )


def test_owner_account_must_be_set() -> None:
    assert owner_account_problem(None, "dev") is not None


def test_owner_account_must_match() -> None:
    assert owner_account_problem("owner", "dev") is not None
    assert owner_account_problem("owner", "owner") is None


def test_owner_check_has_fix_on_failure() -> None:
    result = check_owner_account(_settings("owner"), "dev")
    assert not result.ok
    assert result.fix


def test_full_disk_access_denied(tmp_path: Path) -> None:
    probe = tmp_path / "locked"
    probe.write_bytes(b"")
    probe.chmod(0)
    try:
        result = check_full_disk_access((tmp_path / "absent", probe))
    finally:
        probe.chmod(0o600)
    assert not result.ok
    assert "Full Disk Access" in (result.fix or "")


def test_full_disk_access_granted(tmp_path: Path) -> None:
    probe = tmp_path / "open"
    probe.write_bytes(b"")
    assert check_full_disk_access((tmp_path / "absent", probe)).ok


def test_free_disk_threshold(tmp_path: Path) -> None:
    assert check_free_disk(tmp_path / "missing/data", minimum_gb=0).ok
    assert not check_free_disk(tmp_path, minimum_gb=10**9).ok


def test_mps_unavailable_has_fix() -> None:
    result = check_mps(lambda: False)
    assert not result.ok
    assert result.fix


def test_full_disk_access_unverifiable(tmp_path: Path) -> None:
    assert not check_full_disk_access((tmp_path / "absent",)).ok
