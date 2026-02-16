from __future__ import annotations

import json
import signal
from pathlib import Path

import pytest

from clawbox import watcher as watcher_mod


class _Proc:
    def __init__(self, pid: int, poll_value: int | None = None):
        self.pid = pid
        self._poll_value = poll_value

    def poll(self):
        return self._poll_value


def _record_path(state_dir: Path, vm_name: str) -> Path:
    return state_dir / "watchers" / f"{vm_name}.json"


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_start_vm_watcher_launches_and_writes_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watcher_mod.subprocess, "Popen", lambda *_args, **_kwargs: _Proc(pid=4242))
    monkeypatch.setattr(watcher_mod.time, "sleep", lambda *_args, **_kwargs: None)
    pid = watcher_mod.start_vm_watcher(tmp_path, "clawbox-1", poll_seconds=3)
    assert pid == 4242
    record = _read_json(_record_path(tmp_path, "clawbox-1"))
    assert record["pid"] == 4242
    assert record["poll_seconds"] == 3


def test_start_vm_watcher_reuses_live_existing_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_file = _record_path(tmp_path, "clawbox-1")
    record_file.parent.mkdir(parents=True, exist_ok=True)
    record_file.write_text(
        json.dumps(
            {
                "vm_name": "clawbox-1",
                "pid": 9991,
                "poll_seconds": 2,
                "started_at": "2026-01-01T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(watcher_mod, "_pid_running", lambda _pid: True)
    monkeypatch.setattr(watcher_mod, "_is_watcher_pid", lambda _pid, _vm_name: True)

    called = {"popen": False}

    def _unexpected_popen(*_args, **_kwargs):
        called["popen"] = True
        return _Proc(pid=1)

    monkeypatch.setattr(watcher_mod.subprocess, "Popen", _unexpected_popen)
    pid = watcher_mod.start_vm_watcher(tmp_path, "clawbox-1")
    assert pid == 9991
    assert called["popen"] is False


def test_stop_vm_watcher_signals_and_removes_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_file = _record_path(tmp_path, "clawbox-1")
    record_file.parent.mkdir(parents=True, exist_ok=True)
    record_file.write_text(
        json.dumps(
            {
                "vm_name": "clawbox-1",
                "pid": 7777,
                "poll_seconds": 2,
                "started_at": "2026-01-01T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    signals: list[signal.Signals] = []
    running_checks = {"count": 0}

    def _pid_running(_pid: int) -> bool:
        running_checks["count"] += 1
        return running_checks["count"] == 1

    monkeypatch.setattr(watcher_mod, "_pid_running", _pid_running)
    monkeypatch.setattr(watcher_mod, "_is_watcher_pid", lambda _pid, _vm_name: True)
    monkeypatch.setattr(watcher_mod, "_signal_watcher_pid", lambda _pid, sig: signals.append(sig))
    monkeypatch.setattr(watcher_mod.time, "sleep", lambda *_args, **_kwargs: None)

    stopped = watcher_mod.stop_vm_watcher(tmp_path, "clawbox-1")
    assert stopped is True
    assert signals == [signal.SIGTERM]
    assert not record_file.exists()


def test_reconcile_vm_watchers_stops_dead_vm_watchers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vm_name = "clawbox-2"
    record_file = _record_path(tmp_path, vm_name)
    record_file.parent.mkdir(parents=True, exist_ok=True)
    record_file.write_text(
        json.dumps(
            {
                "vm_name": vm_name,
                "pid": 3333,
                "poll_seconds": 2,
                "started_at": "2026-01-01T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    class _Tart:
        def vm_running(self, _vm_name: str) -> bool:
            return False

    stopped: list[str] = []
    cleaned: list[str] = []
    monkeypatch.setattr(watcher_mod, "_pid_running", lambda _pid: True)
    monkeypatch.setattr(watcher_mod, "stop_vm_watcher", lambda _state_dir, name: stopped.append(name) or True)
    monkeypatch.setattr(watcher_mod, "cleanup_locks_for_vm", lambda name: cleaned.append(name))

    watcher_mod.reconcile_vm_watchers(_Tart(), tmp_path)
    assert stopped == [vm_name]
    assert cleaned == [vm_name]


def test_run_vm_watcher_loop_cleans_locks_and_removes_own_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vm_name = "clawbox-3"
    record_file = _record_path(tmp_path, vm_name)
    record_file.parent.mkdir(parents=True, exist_ok=True)
    record_file.write_text(
        json.dumps(
            {
                "vm_name": vm_name,
                "pid": watcher_mod.os.getpid(),
                "poll_seconds": 1,
                "started_at": "2026-01-01T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    class _Tart:
        def __init__(self):
            self.calls = 0

        def vm_running(self, _vm_name: str) -> bool:
            self.calls += 1
            return self.calls == 1

    cleaned: list[str] = []
    monkeypatch.setattr(watcher_mod, "cleanup_locks_for_vm", lambda name: cleaned.append(name))
    monkeypatch.setattr(watcher_mod.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(watcher_mod.signal, "signal", lambda *_args, **_kwargs: None)

    watcher_mod.run_vm_watcher_loop(tart=_Tart(), state_dir=tmp_path, vm_name=vm_name, poll_seconds=1)
    assert cleaned == [vm_name]
    assert not record_file.exists()
