from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from clawbox import locks as lock_mod


class _Tart:
    def __init__(self):
        self.running: dict[str, bool] = {}

    def vm_running(self, vm_name: str) -> bool:
        return self.running.get(vm_name, False)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def _lock_dir_for_path(spec: lock_mod.LockSpec, path: Path, home: Path) -> Path:
    key = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
    return home / ".clawbox" / "locks" / spec.lock_kind / key


def test_cleanup_other_locks_is_noop_when_root_missing(isolated_home: Path) -> None:
    keep = isolated_home / "keep"
    lock_mod._cleanup_other_locks_for_vm(lock_mod.OPENCLAW_SOURCE_LOCK, "clawbox-91", keep)


def test_locked_path_and_cleanup_handle_non_dirs_and_missing_roots(isolated_home: Path) -> None:
    spec = lock_mod.OPENCLAW_SOURCE_LOCK
    assert lock_mod.locked_path_for_vm(spec, "clawbox-91") == ""

    lock_root = isolated_home / ".clawbox" / "locks" / spec.lock_kind
    lock_root.mkdir(parents=True, exist_ok=True)
    (lock_root / "not-a-dir").write_text("ignored", encoding="utf-8")

    path = isolated_home / "src"
    lock_dir = _lock_dir_for_path(spec, path, isolated_home)
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / "owner_vm").write_text("clawbox-91\n", encoding="utf-8")
    (lock_dir / spec.path_field).write_text(f"{path}\n", encoding="utf-8")

    assert lock_mod.locked_path_for_vm(spec, "clawbox-91") == str(path)
    lock_mod.cleanup_locks_for_vm("clawbox-91")
    assert lock_mod.locked_path_for_vm(spec, "clawbox-91") == ""


def test_acquire_path_lock_same_owner_updates_and_prunes_other_locks(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tart = _Tart()
    monkeypatch.setattr(lock_mod.time, "sleep", lambda *_args, **_kwargs: None)

    path1 = isolated_home / "src1"
    path2 = isolated_home / "src2"
    lock_mod.acquire_path_lock(lock_mod.OPENCLAW_SOURCE_LOCK, "clawbox-91", str(path1), tart)
    lock_mod.acquire_path_lock(lock_mod.OPENCLAW_SOURCE_LOCK, "clawbox-91", str(path2), tart)

    # Re-acquire same canonical path as existing owner to exercise in-place refresh.
    lock_mod.acquire_path_lock(lock_mod.OPENCLAW_SOURCE_LOCK, "clawbox-91", str(path2), tart)

    lock_root = isolated_home / ".clawbox" / "locks" / lock_mod.OPENCLAW_SOURCE_LOCK.lock_kind
    lock_dirs = [entry for entry in lock_root.iterdir() if entry.is_dir()]
    assert len(lock_dirs) == 1
    lock_dir = lock_dirs[0]
    assert (lock_dir / "owner_vm").read_text(encoding="utf-8").strip() == "clawbox-91"
    assert (lock_dir / lock_mod.OPENCLAW_SOURCE_LOCK.path_field).read_text(encoding="utf-8").strip() == str(
        path2.resolve()
    )


def test_acquire_path_lock_reclaims_missing_owner_metadata(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tart = _Tart()
    spec = lock_mod.OPENCLAW_SOURCE_LOCK
    path = isolated_home / "src"
    lock_dir = _lock_dir_for_path(spec, path, isolated_home)
    lock_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(lock_mod.time, "sleep", lambda *_args, **_kwargs: None)

    lock_mod.acquire_path_lock(spec, "clawbox-91", str(path), tart)
    assert (lock_dir / "owner_vm").read_text(encoding="utf-8").strip() == "clawbox-91"


def test_acquire_path_lock_retries_mkdir_oserror_then_fails(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tart = _Tart()
    spec = lock_mod.OPENCLAW_SOURCE_LOCK
    path = isolated_home / "src"
    canonical = path.resolve()
    lock_root = isolated_home / ".clawbox" / "locks" / spec.lock_kind
    lock_dir = _lock_dir_for_path(spec, path, isolated_home)
    real_mkdir = Path.mkdir

    def fake_mkdir(self: Path, *args, **kwargs):
        if self == lock_root:
            return real_mkdir(self, *args, **kwargs)
        if self == lock_dir:
            raise OSError("contended")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    monkeypatch.setattr(lock_mod, "_canonical_path", lambda _path: canonical)
    monkeypatch.setattr(lock_mod.time, "sleep", lambda *_args, **_kwargs: None)

    with pytest.raises(lock_mod.LockError, match="Could not acquire lock"):
        lock_mod.acquire_path_lock(spec, "clawbox-91", str(path), tart)


def test_acquire_path_lock_raises_when_other_owner_vm_is_running(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tart = _Tart()
    tart.running["clawbox-92"] = True
    spec = lock_mod.OPENCLAW_SOURCE_LOCK
    path = isolated_home / "src"
    lock_dir = _lock_dir_for_path(spec, path, isolated_home)
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / "owner_vm").write_text("clawbox-92\n", encoding="utf-8")
    (lock_dir / "owner_host").write_text("host-a\n", encoding="utf-8")
    (lock_dir / spec.path_field).write_text(f"{path.resolve()}\n", encoding="utf-8")
    monkeypatch.setattr(lock_mod.time, "sleep", lambda *_args, **_kwargs: None)

    with pytest.raises(lock_mod.LockError, match="already in use by running VM 'clawbox-92'"):
        lock_mod.acquire_path_lock(spec, "clawbox-91", str(path), tart)
