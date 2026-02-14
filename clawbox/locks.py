from __future__ import annotations

import hashlib
import os
import socket
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from clawbox.tart import TartClient


@dataclass(frozen=True)
class LockSpec:
    lock_kind: str
    path_field: str
    resource_label: str
    arg_hint: str


OPENCLAW_SOURCE_LOCK = LockSpec(
    lock_kind="openclaw-source",
    path_field="source_path",
    resource_label="OpenClaw source",
    arg_hint="--openclaw-source",
)
OPENCLAW_PAYLOAD_LOCK = LockSpec(
    lock_kind="openclaw-payload",
    path_field="payload_path",
    resource_label="OpenClaw payload",
    arg_hint="--openclaw-payload",
)
SIGNAL_PAYLOAD_LOCK = LockSpec(
    lock_kind="signal-payload",
    path_field="payload_path",
    resource_label="Signal payload",
    arg_hint="--signal-cli-payload",
)


class LockError(RuntimeError):
    """Raised when a path lock cannot be acquired."""


ALL_LOCK_SPECS = (
    OPENCLAW_SOURCE_LOCK,
    OPENCLAW_PAYLOAD_LOCK,
    SIGNAL_PAYLOAD_LOCK,
)


def _lock_root(spec: LockSpec) -> Path:
    return Path.home() / ".clawbox" / "locks" / spec.lock_kind


def _canonical_path(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _lock_dir_for(spec: LockSpec, canonical_path: Path) -> Path:
    key = hashlib.sha256(str(canonical_path).encode("utf-8")).hexdigest()
    return _lock_root(spec) / key


def _write_metadata(lock_dir: Path, spec: LockSpec, canonical_path: Path, vm_name: str) -> None:
    host_name = socket.gethostname().split(".")[0] if socket.gethostname() else "unknown-host"
    _atomic_write_text(lock_dir / spec.path_field, f"{canonical_path}\n")
    _atomic_write_text(lock_dir / "owner_vm", f"{vm_name}\n")
    _atomic_write_text(lock_dir / "owner_host", f"{host_name}\n")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _atomic_write_text(lock_dir / "updated_at", f"{now}\n")


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        Path(tmp_name).replace(path)
    finally:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _reclaim_lock_dir(lock_dir: Path) -> None:
    shutil.rmtree(lock_dir, ignore_errors=True)


def _cleanup_other_locks_for_vm(spec: LockSpec, vm_name: str, keep_lock_dir: Path) -> None:
    lock_root = _lock_root(spec)
    if not lock_root.exists():
        return
    for lock_dir in lock_root.iterdir():
        if not lock_dir.is_dir() or lock_dir == keep_lock_dir:
            continue
        owner_vm = _read_text(lock_dir / "owner_vm")
        if owner_vm != vm_name:
            continue
        shutil.rmtree(lock_dir, ignore_errors=True)


def acquire_path_lock(
    spec: LockSpec,
    vm_name: str,
    resource_path: str,
    tart: TartClient,
) -> None:
    canonical = _canonical_path(resource_path)
    lock_root = _lock_root(spec)
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_dir = _lock_dir_for(spec, canonical)
    max_attempts = 12

    for attempt in range(1, max_attempts + 1):
        try:
            lock_dir.mkdir()
            _write_metadata(lock_dir, spec, canonical, vm_name)
            _cleanup_other_locks_for_vm(spec, vm_name, keep_lock_dir=lock_dir)
            return
        except FileExistsError:
            pass
        except OSError:
            if attempt == max_attempts:
                break
            time.sleep(0.1)
            continue

        owner_vm = _read_text(lock_dir / "owner_vm")
        owner_host = _read_text(lock_dir / "owner_host")
        owner_path = _read_text(lock_dir / spec.path_field)

        if not owner_vm:
            # Another process may still be writing metadata; wait briefly before reclaim.
            if attempt <= 3:
                time.sleep(0.1)
                continue
            _reclaim_lock_dir(lock_dir)
            continue

        if owner_vm == vm_name:
            _write_metadata(lock_dir, spec, canonical, vm_name)
            _cleanup_other_locks_for_vm(spec, vm_name, keep_lock_dir=lock_dir)
            return

        if tart.vm_running(owner_vm):
            raise LockError(
                f"Error: {spec.resource_label} is already in use by running VM '{owner_vm}'.\n"
                f"  path: {owner_path or canonical}\n"
                f"  owner host: {owner_host or 'unknown'}\n"
                f"Use a different {spec.arg_hint} path or run "
                "clawbox down on the owner VM first."
            )

        _reclaim_lock_dir(lock_dir)
        time.sleep(0.05)

    raise LockError(
        f"Error: Could not acquire lock for {spec.resource_label}.\n"
        "The lock directory was contended by concurrent operations. Retry the command."
    )


def cleanup_locks_for_vm(vm_name: str) -> None:
    for spec in ALL_LOCK_SPECS:
        lock_root = _lock_root(spec)
        if not lock_root.exists():
            continue
        for lock_dir in lock_root.iterdir():
            if not lock_dir.is_dir():
                continue
            owner_vm_file = lock_dir / "owner_vm"
            owner_vm = _read_text(owner_vm_file)
            if owner_vm != vm_name:
                continue
            shutil.rmtree(lock_dir, ignore_errors=True)
