from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from clawbox.io_utils import atomic_write_text, read_text_or_empty, tail_lines
from clawbox.locks import cleanup_locks_for_vm
from clawbox.mutagen import MutagenError, teardown_vm_sync
from clawbox.tart import TartClient, TartError


class WatcherError(RuntimeError):
    """Raised for watcher lifecycle failures."""


@dataclass(frozen=True)
class WatcherRecord:
    vm_name: str
    pid: int
    poll_seconds: int
    started_at: str


def _watchers_dir(state_dir: Path) -> Path:
    return state_dir / "watchers"


def _watcher_record_path(state_dir: Path, vm_name: str) -> Path:
    return _watchers_dir(state_dir) / f"{vm_name}.json"


def _watcher_log_path(state_dir: Path, vm_name: str) -> Path:
    return state_dir / "logs" / f"{vm_name}.watcher.log"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    atomic_write_text(path, json.dumps(payload, sort_keys=True) + "\n")


def _read_record(path: Path) -> WatcherRecord | None:
    raw = read_text_or_empty(path)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    vm_name = payload.get("vm_name")
    pid = payload.get("pid")
    poll_seconds = payload.get("poll_seconds")
    started_at = payload.get("started_at")
    if (
        not isinstance(vm_name, str)
        or not isinstance(pid, int)
        or not isinstance(poll_seconds, int)
        or not isinstance(started_at, str)
    ):
        return None
    if not vm_name or pid <= 0 or poll_seconds <= 0:
        return None
    return WatcherRecord(
        vm_name=vm_name,
        pid=pid,
        poll_seconds=poll_seconds,
        started_at=started_at,
    )


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_cmdline(pid: int) -> str:
    if not _pid_running(pid):
        return ""
    try:
        proc = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError:
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _is_watcher_pid(pid: int, vm_name: str) -> bool:
    cmd = _pid_cmdline(pid)
    if not cmd:
        return False
    try:
        parts = set(shlex.split(cmd))
    except ValueError:
        return "_watch-vm" in cmd and vm_name in cmd
    return "_watch-vm" in parts and vm_name in parts


def _signal_watcher_pid(pid: int, sig: signal.Signals) -> None:
    if pid <= 0:
        return
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return
    except OSError:
        pass
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return
    except OSError:
        pass


def _write_record(state_dir: Path, record: WatcherRecord) -> None:
    _atomic_write_json(
        _watcher_record_path(state_dir, record.vm_name),
        {
            "vm_name": record.vm_name,
            "pid": record.pid,
            "poll_seconds": record.poll_seconds,
            "started_at": record.started_at,
        },
    )


def _remove_record_if_owner(state_dir: Path, vm_name: str, pid: int) -> None:
    record_path = _watcher_record_path(state_dir, vm_name)
    record = _read_record(record_path)
    if record is None:
        record_path.unlink(missing_ok=True)
        return
    if record.pid == pid:
        record_path.unlink(missing_ok=True)


def start_vm_watcher(state_dir: Path, vm_name: str, *, poll_seconds: int = 2) -> int:
    if poll_seconds <= 0:
        raise WatcherError("watcher poll_seconds must be > 0")

    record_path = _watcher_record_path(state_dir, vm_name)
    existing = _read_record(record_path)
    if existing is not None and _pid_running(existing.pid) and _is_watcher_pid(existing.pid, vm_name):
        return existing.pid
    if existing is not None:
        record_path.unlink(missing_ok=True)

    log_file = _watcher_log_path(state_dir, vm_name)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_file.open("wb")
    cmd = [
        sys.executable,
        "-m",
        "clawbox.main",
        "_watch-vm",
        vm_name,
        "--state-dir",
        str(state_dir),
        "--poll-seconds",
        str(poll_seconds),
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise WatcherError(f"Error: Could not find Python executable: {sys.executable}") from exc
    except OSError as exc:
        raise WatcherError(f"Error: Could not launch watcher for '{vm_name}': {exc}") from exc
    finally:
        log_handle.close()

    time.sleep(0.15)
    if proc.poll() is not None:
        msg = [f"Error: watcher failed to start for '{vm_name}'."]
        tail = tail_lines(log_file)
        if tail:
            msg.append(f"Recent watcher output ({log_file}):")
            msg.append(tail)
        raise WatcherError("\n".join(msg))

    _write_record(
        state_dir,
        WatcherRecord(
            vm_name=vm_name,
            pid=proc.pid,
            poll_seconds=poll_seconds,
            started_at=_timestamp(),
        ),
    )
    return proc.pid


def stop_vm_watcher(state_dir: Path, vm_name: str, *, timeout_seconds: int = 5) -> bool:
    record_path = _watcher_record_path(state_dir, vm_name)
    record = _read_record(record_path)
    if record is None:
        record_path.unlink(missing_ok=True)
        return False

    if _is_watcher_pid(record.pid, vm_name):
        _signal_watcher_pid(record.pid, signal.SIGTERM)
        deadline = time.monotonic() + max(timeout_seconds, 0)
        while time.monotonic() < deadline:
            if not _pid_running(record.pid):
                break
            time.sleep(0.1)
        if _pid_running(record.pid):
            _signal_watcher_pid(record.pid, signal.SIGKILL)
    record_path.unlink(missing_ok=True)
    return True


def reconcile_vm_watchers(tart: TartClient, state_dir: Path) -> None:
    def _vm_running(name: str) -> bool | None:
        try:
            return tart.vm_running(name)
        except TartError:
            return None

    watchers_dir = _watchers_dir(state_dir)
    if not watchers_dir.exists():
        return
    for record_path in watchers_dir.glob("*.json"):
        record = _read_record(record_path)
        if record is None:
            record_path.unlink(missing_ok=True)
            continue
        if not _pid_running(record.pid):
            record_path.unlink(missing_ok=True)
            running = _vm_running(record.vm_name)
            if running is False:
                cleanup_locks_for_vm(record.vm_name)
            continue
        running = _vm_running(record.vm_name)
        if running is False:
            stop_vm_watcher(state_dir, record.vm_name)
            cleanup_locks_for_vm(record.vm_name)


def run_vm_watcher_loop(
    *,
    tart: TartClient,
    state_dir: Path,
    vm_name: str,
    poll_seconds: int = 2,
) -> None:
    should_exit = False

    def _handle_signal(_sig: int, _frame: object) -> None:
        nonlocal should_exit
        should_exit = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        while not should_exit:
            try:
                running = tart.vm_running(vm_name)
            except TartError:
                time.sleep(poll_seconds)
                continue

            if not running:
                try:
                    teardown_vm_sync(state_dir, vm_name, flush=False)
                except MutagenError:
                    pass
                cleanup_locks_for_vm(vm_name)
                break
            time.sleep(poll_seconds)
    finally:
        _remove_record_if_owner(state_dir, vm_name, os.getpid())
