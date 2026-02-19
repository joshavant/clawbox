from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from clawbox import tart as tart_mod
from clawbox.tart import TartClient, TartError, wait_for_vm_running


def _cp(*, args: list[str], rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=rc, stdout=stdout, stderr=stderr)


def test_run_maps_command_not_found(monkeypatch: pytest.MonkeyPatch):
    client = TartClient()

    def raise_not_found(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(tart_mod.subprocess, "run", raise_not_found)
    with pytest.raises(TartError, match="Command not found: tart"):
        client._run(["tart", "list"])


def test_run_maps_oserror(monkeypatch: pytest.MonkeyPatch):
    client = TartClient()

    def raise_oserror(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(tart_mod.subprocess, "run", raise_oserror)
    with pytest.raises(TartError, match="Could not run command 'tart list'"):
        client._run(["tart", "list"])


def test_run_surfaces_stderr_for_nonzero(monkeypatch: pytest.MonkeyPatch):
    client = TartClient()
    monkeypatch.setattr(
        tart_mod.subprocess,
        "run",
        lambda *args, **kwargs: _cp(args=["tart", "list"], rc=7, stderr="failed"),
    )
    with pytest.raises(TartError, match="exit 7"):
        client._run(["tart", "list"])


def test_run_surfaces_stdout_when_stderr_empty(monkeypatch: pytest.MonkeyPatch):
    client = TartClient()
    monkeypatch.setattr(
        tart_mod.subprocess,
        "run",
        lambda *args, **kwargs: _cp(args=["tart", "list"], rc=3, stdout="stdout details"),
    )
    with pytest.raises(TartError, match="stdout details"):
        client._run(["tart", "list"])


def test_list_vms_json_parse_error(monkeypatch: pytest.MonkeyPatch):
    client = TartClient()
    monkeypatch.setattr(client, "_run", lambda *args, **kwargs: _cp(args=["tart"], stdout="{"))
    with pytest.raises(TartError, match="Could not parse tart list output"):
        client.list_vms_json()


def test_list_vms_json_requires_list(monkeypatch: pytest.MonkeyPatch):
    client = TartClient()
    monkeypatch.setattr(client, "_run", lambda *args, **kwargs: _cp(args=["tart"], stdout=json.dumps({})))
    with pytest.raises(TartError, match="expected a JSON list"):
        client.list_vms_json()


def test_vm_exists_and_running(monkeypatch: pytest.MonkeyPatch):
    client = TartClient()
    monkeypatch.setattr(
        client,
        "list_vms_json",
        lambda: [
            {"Name": "clawbox-91", "Running": True},
            {"Name": "clawbox-92", "Running": False},
        ],
    )
    assert client.vm_exists("clawbox-91") is True
    assert client.vm_exists("clawbox-99") is False
    assert client.vm_running("clawbox-91") is True
    assert client.vm_running("clawbox-92") is False


def test_ip_uses_agent_then_default(monkeypatch: pytest.MonkeyPatch):
    client = TartClient()
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if "--resolver=agent" in args:
            return _cp(args=args, rc=1, stdout="")
        return _cp(args=args, rc=0, stdout="192.168.64.22\n")

    monkeypatch.setattr(client, "_run", fake_run)
    assert client.ip("clawbox-91") == "192.168.64.22"
    assert calls[0] == ["tart", "ip", "--resolver=agent", "clawbox-91"]
    assert calls[1] == ["tart", "ip", "clawbox-91"]


def test_ip_returns_none_when_all_resolvers_fail(monkeypatch: pytest.MonkeyPatch):
    client = TartClient()
    monkeypatch.setattr(client, "_run", lambda *args, **kwargs: _cp(args=["tart"], rc=1, stdout=""))
    assert client.ip("clawbox-91") is None


def test_run_in_background_starts_tart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    client = TartClient()

    class Proc:
        pid = 4242

    def fake_popen(args, **kwargs):
        assert args[:3] == ["tart", "run", "clawbox-91"]
        return Proc()

    monkeypatch.setattr(tart_mod.subprocess, "Popen", fake_popen)
    proc = client.run_in_background("clawbox-91", ["--no-graphics"], tmp_path / "launch.log")
    assert proc.pid == 4242


def test_run_in_background_maps_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    client = TartClient()

    def raise_not_found(*args, **kwargs):
        raise FileNotFoundError("missing tart")

    monkeypatch.setattr(tart_mod.subprocess, "Popen", raise_not_found)
    with pytest.raises(TartError, match="Command not found: tart"):
        client.run_in_background("clawbox-91", [], tmp_path / "launch.log")


def test_wait_for_vm_running_success_and_timeout(monkeypatch: pytest.MonkeyPatch):
    class FakeTart:
        def __init__(self):
            self.calls = 0

        def vm_running(self, _vm_name: str) -> bool:
            self.calls += 1
            return self.calls >= 2

    tart = FakeTart()
    monkeypatch.setattr(tart_mod.time, "sleep", lambda *_args, **_kwargs: None)
    assert wait_for_vm_running(tart, "clawbox-91", timeout_seconds=3, poll_seconds=1) is True

    class AlwaysOffTart:
        def vm_running(self, _vm_name: str) -> bool:
            return False

    assert wait_for_vm_running(AlwaysOffTart(), "clawbox-91", timeout_seconds=1, poll_seconds=1) is False
