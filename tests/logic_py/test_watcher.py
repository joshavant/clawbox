from __future__ import annotations

import json
import subprocess
import signal
from pathlib import Path

import pytest

from clawbox import watcher as watcher_mod
from clawbox.mutagen import MutagenError
from clawbox.tart import TartError


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


def test_read_record_invalid_payloads_return_none(tmp_path: Path) -> None:
    record_file = _record_path(tmp_path, "clawbox-1")
    record_file.parent.mkdir(parents=True, exist_ok=True)

    record_file.write_text("not-json", encoding="utf-8")
    assert watcher_mod._read_record(record_file) is None

    record_file.write_text("[]", encoding="utf-8")
    assert watcher_mod._read_record(record_file) is None

    record_file.write_text(
        json.dumps({"vm_name": "", "pid": -1, "poll_seconds": 0, "started_at": 1}) + "\n",
        encoding="utf-8",
    )
    assert watcher_mod._read_record(record_file) is None


def test_pid_running_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    assert watcher_mod._pid_running(0) is False

    monkeypatch.setattr(watcher_mod.os, "kill", lambda *_args, **_kwargs: (_ for _ in ()).throw(ProcessLookupError()))
    assert watcher_mod._pid_running(1234) is False

    monkeypatch.setattr(watcher_mod.os, "kill", lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError()))
    assert watcher_mod._pid_running(1234) is True

    monkeypatch.setattr(watcher_mod.os, "kill", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("boom")))
    assert watcher_mod._pid_running(1234) is False


def test_pid_cmdline_and_is_watcher_pid_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watcher_mod, "_pid_running", lambda _pid: False)
    assert watcher_mod._pid_cmdline(101) == ""

    monkeypatch.setattr(watcher_mod, "_pid_running", lambda _pid: True)
    monkeypatch.setattr(
        watcher_mod.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("ps missing")),
    )
    assert watcher_mod._pid_cmdline(101) == ""

    monkeypatch.setattr(
        watcher_mod,
        "_pid_cmdline",
        lambda _pid: "python -m clawbox.main _watch-vm \"unterminated clawbox-1",
    )
    assert watcher_mod._is_watcher_pid(101, "clawbox-1") is True


def test_signal_watcher_pid_error_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watcher_mod.os, "getpgid", lambda _pid: (_ for _ in ()).throw(ProcessLookupError()))
    watcher_mod._signal_watcher_pid(1234, signal.SIGTERM)

    monkeypatch.setattr(watcher_mod.os, "getpgid", lambda _pid: 777)
    monkeypatch.setattr(watcher_mod.os, "killpg", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no pg")))
    monkeypatch.setattr(watcher_mod.os, "kill", lambda *_args, **_kwargs: (_ for _ in ()).throw(ProcessLookupError()))
    watcher_mod._signal_watcher_pid(1234, signal.SIGTERM)


def test_start_vm_watcher_rejects_nonpositive_poll_seconds(tmp_path: Path) -> None:
    with pytest.raises(watcher_mod.WatcherError, match="poll_seconds must be > 0"):
        watcher_mod.start_vm_watcher(tmp_path, "clawbox-1", poll_seconds=0)


def test_start_vm_watcher_maps_process_launch_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        watcher_mod.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    with pytest.raises(watcher_mod.WatcherError, match="Could not find Python executable"):
        watcher_mod.start_vm_watcher(tmp_path, "clawbox-1")

    monkeypatch.setattr(
        watcher_mod.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cannot fork")),
    )
    with pytest.raises(watcher_mod.WatcherError, match="Could not launch watcher"):
        watcher_mod.start_vm_watcher(tmp_path, "clawbox-1")


def test_start_vm_watcher_surfaces_early_exit_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watcher_mod.subprocess, "Popen", lambda *_args, **_kwargs: _Proc(pid=55, poll_value=1))
    monkeypatch.setattr(watcher_mod.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(watcher_mod, "tail_lines", lambda *_args, **_kwargs: "watcher failed")

    with pytest.raises(watcher_mod.WatcherError, match="watcher failed to start"):
        watcher_mod.start_vm_watcher(tmp_path, "clawbox-1")


def test_stop_vm_watcher_no_record_returns_false(tmp_path: Path) -> None:
    assert watcher_mod.stop_vm_watcher(tmp_path, "clawbox-404") is False


def test_stop_vm_watcher_escalates_to_sigkill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_file = _record_path(tmp_path, "clawbox-1")
    record_file.parent.mkdir(parents=True, exist_ok=True)
    record_file.write_text(
        json.dumps(
            {
                "vm_name": "clawbox-1",
                "pid": 9999,
                "poll_seconds": 2,
                "started_at": "2026-01-01T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    seen_signals: list[signal.Signals] = []
    monkeypatch.setattr(watcher_mod, "_pid_running", lambda _pid: True)
    monkeypatch.setattr(watcher_mod, "_is_watcher_pid", lambda _pid, _vm_name: True)
    monkeypatch.setattr(watcher_mod, "_signal_watcher_pid", lambda _pid, sig: seen_signals.append(sig))
    monkeypatch.setattr(watcher_mod.time, "sleep", lambda *_args, **_kwargs: None)

    assert watcher_mod.stop_vm_watcher(tmp_path, "clawbox-1", timeout_seconds=0) is True
    assert seen_signals == [signal.SIGTERM, signal.SIGKILL]


def test_reconcile_vm_watchers_handles_invalid_records_and_dead_pids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watchers_dir = tmp_path / "watchers"
    watchers_dir.mkdir(parents=True, exist_ok=True)
    bad_record = watchers_dir / "bad.json"
    bad_record.write_text("not-json", encoding="utf-8")

    dead_record = _record_path(tmp_path, "clawbox-2")
    dead_record.write_text(
        json.dumps(
            {
                "vm_name": "clawbox-2",
                "pid": 2222,
                "poll_seconds": 2,
                "started_at": "2026-01-01T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    class _Tart:
        def vm_running(self, vm_name: str) -> bool:
            return vm_name == "clawbox-1"

    monkeypatch.setattr(watcher_mod, "_pid_running", lambda _pid: False)
    cleaned: list[str] = []
    monkeypatch.setattr(watcher_mod, "cleanup_locks_for_vm", lambda name: cleaned.append(name))

    watcher_mod.reconcile_vm_watchers(_Tart(), tmp_path)
    assert cleaned == ["clawbox-2"]
    assert not bad_record.exists()
    assert not dead_record.exists()


def test_reconcile_vm_watchers_ignores_tart_errors_for_dead_pids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_file = _record_path(tmp_path, "clawbox-9")
    record_file.parent.mkdir(parents=True, exist_ok=True)
    record_file.write_text(
        json.dumps(
            {
                "vm_name": "clawbox-9",
                "pid": 999,
                "poll_seconds": 2,
                "started_at": "2026-01-01T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    class _Tart:
        def vm_running(self, _vm_name: str) -> bool:
            raise TartError("tart unavailable")

    monkeypatch.setattr(watcher_mod, "_pid_running", lambda _pid: False)
    cleaned: list[str] = []
    monkeypatch.setattr(watcher_mod, "cleanup_locks_for_vm", lambda name: cleaned.append(name))

    watcher_mod.reconcile_vm_watchers(_Tart(), tmp_path)
    assert cleaned == []
    assert not record_file.exists()


def test_run_vm_watcher_loop_handles_tart_errors_and_mutagen_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vm_name = "clawbox-7"
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
            if self.calls == 1:
                raise TartError("transient")
            return False

    monkeypatch.setattr(watcher_mod.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(watcher_mod.signal, "signal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        watcher_mod,
        "teardown_vm_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(MutagenError("mutagen fail")),
    )
    cleaned: list[str] = []
    monkeypatch.setattr(watcher_mod, "cleanup_locks_for_vm", lambda name: cleaned.append(name))

    watcher_mod.run_vm_watcher_loop(tart=_Tart(), state_dir=tmp_path, vm_name=vm_name, poll_seconds=1)
    assert cleaned == [vm_name]
    assert not record_file.exists()
