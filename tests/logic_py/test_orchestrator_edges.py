from __future__ import annotations

import io
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from clawbox import orchestrator
from clawbox.locks import LockError
from clawbox.orchestrator import ProvisionOptions, UpOptions, UserFacingError
from clawbox.tart import TartError


class DummyProcess:
    def __init__(self, pid: int = 1000):
        self.pid = pid

    def poll(self):
        return None


class FakeTart:
    def __init__(self):
        self.exists: dict[str, bool] = {}
        self.running: dict[str, bool] = {}
        self.ip_map: dict[str, str | None] = {}
        self.stop_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.next_proc = DummyProcess()

    def vm_exists(self, vm_name: str) -> bool:
        return self.exists.get(vm_name, False)

    def vm_running(self, vm_name: str) -> bool:
        return self.running.get(vm_name, False)

    def clone(self, _base_image: str, vm_name: str) -> None:
        self.exists[vm_name] = True

    def run_in_background(self, _vm_name: str, _run_args: list[str], _log_file: Path):
        return self.next_proc

    def stop(self, vm_name: str) -> None:
        self.stop_calls.append(vm_name)
        self.running[vm_name] = False

    def delete(self, vm_name: str) -> None:
        self.delete_calls.append(vm_name)

    def ip(self, vm_name: str) -> str | None:
        return self.ip_map.get(vm_name)


@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(orchestrator, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(orchestrator, "ANSIBLE_DIR", tmp_path / "ansible")
    monkeypatch.setattr(orchestrator, "SECRETS_FILE", tmp_path / "ansible" / "secrets.yml")
    monkeypatch.setattr(orchestrator, "STATE_DIR", tmp_path / ".clawbox" / "state")
    (tmp_path / "ansible").mkdir(parents=True, exist_ok=True)
    yield tmp_path


def _capture_stdout(fn):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    return buf.getvalue()


def test_env_int_invalid_returns_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLAWBOX_TEST_INT", "not-an-int")
    assert orchestrator._env_int("CLAWBOX_TEST_INT", 42) == 42


def test_ensure_secrets_file_maps_missing_error(isolated_paths, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        orchestrator,
        "ensure_vm_password_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )
    with pytest.raises(UserFacingError, match="Secrets file not found"):
        orchestrator.ensure_secrets_file(create_if_missing=False)


def test_ensure_secrets_file_maps_oserror(isolated_paths, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        orchestrator,
        "ensure_vm_password_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("denied")),
    )
    with pytest.raises(UserFacingError, match="Could not write secrets file"):
        orchestrator.ensure_secrets_file(create_if_missing=True)


def test_tail_lines_reads_last_lines(isolated_paths):
    path = isolated_paths / "tail.log"
    path.write_text("1\n2\n3\n", encoding="utf-8")
    assert orchestrator._tail_lines(path, 2) == "2\n3"


def test_validate_profile_rejects_invalid():
    with pytest.raises(UserFacingError, match="--profile must be"):
        orchestrator._validate_profile("bad-profile")


def test_validate_dirs_rejects_missing(tmp_path: Path):
    with pytest.raises(UserFacingError, match="Expected directory does not exist"):
        orchestrator._validate_dirs([str(tmp_path / "missing")])


def test_signal_payload_host_marker_maps_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    marker_dir = tmp_path / "payload"
    marker_dir.mkdir(parents=True, exist_ok=True)

    def raise_oserror(*_args, **_kwargs):
        raise OSError("read only")

    monkeypatch.setattr(Path, "write_text", raise_oserror)
    with pytest.raises(UserFacingError, match="Could not write signal payload marker file"):
        orchestrator._ensure_signal_payload_host_marker(str(marker_dir), "clawbox-1")


def test_acquire_locks_maps_lock_error(monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    monkeypatch.setattr(
        orchestrator,
        "acquire_path_lock",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(LockError("lock held")),
    )
    with pytest.raises(UserFacingError, match="lock held"):
        orchestrator._acquire_locks(tart, "clawbox-1", "/src", "", "")


def test_launch_vm_maps_tart_launch_error(isolated_paths, monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    tart.exists["clawbox-1"] = True
    monkeypatch.setattr(orchestrator, "_acquire_locks", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        tart,
        "run_in_background",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TartError("run failed")),
    )
    with pytest.raises(UserFacingError, match="Failed to launch VM"):
        orchestrator.launch_vm(
            vm_number=1,
            profile="standard",
            openclaw_source="",
            openclaw_payload="",
            signal_payload="",
            headless=False,
            tart=tart,
        )


def test_resolve_vm_ip_returns_when_available():
    tart = FakeTart()
    tart.ip_map["clawbox-1"] = "192.168.64.55"
    assert orchestrator._resolve_vm_ip(tart, "clawbox-1", 1) == "192.168.64.55"


def test_preflight_developer_mounts_success(isolated_paths, monkeypatch: pytest.MonkeyPatch):
    payload_dir = isolated_paths / "payload"
    payload_dir.mkdir(parents=True, exist_ok=True)

    def fake_wait(*_args, **_kwargs):
        statuses = {path: "ok" for path in _kwargs["paths"]}
        return True, statuses, ""

    monkeypatch.setattr(orchestrator, "_wait_for_remote_probe", fake_wait)
    out = _capture_stdout(
        lambda: orchestrator._preflight_developer_mounts(
            "clawbox-1",
            vm_number=1,
            openclaw_payload_host=str(payload_dir),
            signal_payload_host="",
            include_signal_payload=False,
            timeout_seconds=3,
        )
    )
    assert "shared folder mounts verified" in out


def test_preflight_developer_mounts_failure_contains_diagnostics(
    isolated_paths, monkeypatch: pytest.MonkeyPatch
):
    payload_dir = isolated_paths / "payload"
    payload_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        orchestrator,
        "_wait_for_remote_probe",
        lambda *_args, **_kwargs: (False, {path: "missing" for path in _kwargs["paths"]}, "boom"),
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_remote_path_probe",
        lambda *_args, **_kwargs: (
            0,
            {orchestrator.OPENCLAW_SOURCE_MOUNT: "missing", orchestrator.OPENCLAW_PAYLOAD_MOUNT: "missing"},
            "",
        ),
    )
    with pytest.raises(UserFacingError, match="Required shared folders failed preflight checks"):
        orchestrator._preflight_developer_mounts(
            "clawbox-1",
            vm_number=1,
            openclaw_payload_host=str(payload_dir),
            signal_payload_host="",
            include_signal_payload=False,
            timeout_seconds=3,
        )


def test_preflight_signal_payload_marker_success(monkeypatch: pytest.MonkeyPatch):
    marker_path = f"{orchestrator.SIGNAL_PAYLOAD_MOUNT}/{orchestrator.SIGNAL_PAYLOAD_MARKER_FILENAME}"
    monkeypatch.setattr(
        orchestrator,
        "_wait_for_remote_probe",
        lambda *_args, **_kwargs: (True, {marker_path: "ok"}, ""),
    )
    out = _capture_stdout(
        lambda: orchestrator._preflight_signal_payload_marker(
            "clawbox-1",
            vm_number=1,
            timeout_seconds=3,
            inventory_path="192.168.64.1,",
            target_host="192.168.64.1",
        )
    )
    assert "signal-cli payload marker verified" in out


def test_provision_vm_maps_missing_ansible_playbook(isolated_paths, monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    vm_name = "clawbox-1"
    tart.exists[vm_name] = True
    tart.running[vm_name] = True
    tart.ip_map[vm_name] = "192.168.64.10"

    monkeypatch.setattr(orchestrator, "ensure_secrets_file", lambda *_args, **_kwargs: None)

    def raise_not_found(*_args, **_kwargs):
        raise FileNotFoundError("missing ansible-playbook")

    monkeypatch.setattr(orchestrator.subprocess, "run", raise_not_found)
    with pytest.raises(UserFacingError, match="Command not found: ansible-playbook"):
        orchestrator.provision_vm(
            ProvisionOptions(
                vm_number=1,
                profile="standard",
                enable_playwright=False,
                enable_tailscale=False,
                enable_signal_cli=False,
                enable_signal_payload=False,
            ),
            tart,
        )


def test_stop_vm_and_wait_timeout(monkeypatch: pytest.MonkeyPatch):
    class NeverStops(FakeTart):
        def stop(self, vm_name: str) -> None:
            self.stop_calls.append(vm_name)
            self.running[vm_name] = True

    tart = NeverStops()
    tart.running["clawbox-1"] = True
    monkeypatch.setattr(orchestrator.time, "sleep", lambda *_args, **_kwargs: None)
    assert orchestrator._stop_vm_and_wait(tart, "clawbox-1", timeout_seconds=2) is False


def test_render_up_command_includes_optional_flags():
    cmd = orchestrator._render_up_command(
        UpOptions(
            vm_number=2,
            profile="developer",
            openclaw_source="/src",
            openclaw_payload="/payload",
            signal_payload="/signal",
            enable_playwright=True,
            enable_tailscale=True,
            enable_signal_cli=True,
        )
    )
    assert "--add-playwright-provisioning" in cmd
    assert "--add-tailscale-provisioning" in cmd
    assert "--add-signal-cli-provisioning" in cmd
    assert "--signal-cli-payload" in cmd


def test_compute_up_provision_reason_created_vm():
    opts = UpOptions(
        vm_number=1,
        profile="standard",
        openclaw_source="",
        openclaw_payload="",
        signal_payload="",
        enable_playwright=False,
        enable_tailscale=False,
        enable_signal_cli=False,
    )
    reason = orchestrator._compute_up_provision_reason(opts, Path("/tmp/nope"), True, False)
    assert reason == "VM was created in this run"


def test_compute_up_provision_reason_parse_failure(isolated_paths):
    marker_file = orchestrator.STATE_DIR / "clawbox-1.provisioned"
    marker_file.parent.mkdir(parents=True, exist_ok=True)
    marker_file.write_text("bad content\n", encoding="utf-8")
    opts = UpOptions(
        vm_number=1,
        profile="standard",
        openclaw_source="",
        openclaw_payload="",
        signal_payload="",
        enable_playwright=False,
        enable_tailscale=False,
        enable_signal_cli=False,
    )
    with pytest.raises(UserFacingError, match="could not be parsed"):
        orchestrator._compute_up_provision_reason(opts, marker_file, False, False)


def test_ensure_vm_running_for_up_timeout(monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    monkeypatch.setattr(orchestrator, "launch_vm", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "wait_for_vm_running", lambda *_args, **_kwargs: False)
    with pytest.raises(UserFacingError, match="did not transition to running state"):
        orchestrator._ensure_vm_running_for_up(
            "clawbox-1",
            UpOptions(
                vm_number=1,
                profile="standard",
                openclaw_source="",
                openclaw_payload="",
                signal_payload="",
                enable_playwright=False,
                enable_tailscale=False,
                enable_signal_cli=False,
            ),
            "needs provision",
            tart,
        )


def test_relaunch_gui_after_headless_provision_stop_timeout(monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    tart.running["clawbox-1"] = True
    monkeypatch.setattr(orchestrator, "_stop_vm_and_wait", lambda *_args, **_kwargs: False)
    with pytest.raises(UserFacingError, match="Timed out stopping headless VM"):
        orchestrator._relaunch_gui_after_headless_provision(
            "clawbox-1",
            UpOptions(
                vm_number=1,
                profile="standard",
                openclaw_source="",
                openclaw_payload="",
                signal_payload="",
                enable_playwright=False,
                enable_tailscale=False,
                enable_signal_cli=False,
            ),
            tart,
            launched_headless=True,
        )


def test_relaunch_gui_after_headless_provision_relaunch_timeout(monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    tart.running["clawbox-1"] = True
    monkeypatch.setattr(orchestrator, "_stop_vm_and_wait", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(orchestrator, "launch_vm", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "wait_for_vm_running", lambda *_args, **_kwargs: False)
    with pytest.raises(UserFacingError, match="after GUI relaunch"):
        orchestrator._relaunch_gui_after_headless_provision(
            "clawbox-1",
            UpOptions(
                vm_number=1,
                profile="standard",
                openclaw_source="",
                openclaw_payload="",
                signal_payload="",
                enable_playwright=False,
                enable_tailscale=False,
                enable_signal_cli=False,
            ),
            tart,
            launched_headless=True,
        )


def test_ensure_running_after_provision_launches(monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    calls = {"launch": 0, "wait": 0}

    def fake_wait(*_args, **_kwargs):
        calls["wait"] += 1
        return calls["wait"] > 1

    monkeypatch.setattr(orchestrator, "wait_for_vm_running", fake_wait)
    monkeypatch.setattr(orchestrator, "launch_vm", lambda *_args, **_kwargs: calls.update(launch=1))
    orchestrator._ensure_running_after_provision_if_needed(
        "clawbox-1",
        UpOptions(
            vm_number=1,
            profile="standard",
            openclaw_source="",
            openclaw_payload="",
            signal_payload="",
            enable_playwright=False,
            enable_tailscale=False,
            enable_signal_cli=False,
        ),
        tart,
        provision_ran=True,
    )
    assert calls["launch"] == 1


def test_up_errors_when_vm_missing_after_create(isolated_paths, monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    monkeypatch.setattr(orchestrator, "ensure_secrets_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "create_vm", lambda *_args, **_kwargs: None)
    with pytest.raises(UserFacingError, match="was not found after create_vm completed"):
        orchestrator.up(
            UpOptions(
                vm_number=1,
                profile="standard",
                openclaw_source="",
                openclaw_payload="",
                signal_payload="",
                enable_playwright=False,
                enable_tailscale=False,
                enable_signal_cli=False,
            ),
            tart,
        )


def test_up_errors_when_not_running_after_orchestration(isolated_paths, monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    tart.exists["clawbox-1"] = True
    monkeypatch.setattr(orchestrator, "ensure_secrets_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_compute_up_provision_reason", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(orchestrator, "_ensure_vm_running_for_up", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(orchestrator, "_ensure_running_after_provision_if_needed", lambda *_args, **_kwargs: None)
    with pytest.raises(UserFacingError, match="is not running after orchestration"):
        orchestrator.up(
            UpOptions(
                vm_number=1,
                profile="standard",
                openclaw_source="",
                openclaw_payload="",
                signal_payload="",
                enable_playwright=False,
                enable_tailscale=False,
                enable_signal_cli=False,
            ),
            tart,
        )


def test_wait_for_vm_absent_timeout(monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    tart.exists["clawbox-1"] = True
    monkeypatch.setattr(orchestrator.time, "sleep", lambda *_args, **_kwargs: None)
    assert orchestrator._wait_for_vm_absent(tart, "clawbox-1", timeout_seconds=2) is False


def test_down_vm_nonexistent_cleans_locks(isolated_paths, monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    cleaned: list[str] = []
    monkeypatch.setattr(orchestrator, "cleanup_locks_for_vm", lambda vm_name: cleaned.append(vm_name))
    out = _capture_stdout(lambda: orchestrator.down_vm(1, tart))
    assert "does not exist" in out
    assert cleaned == ["clawbox-1"]


def test_down_vm_timeout_raises(monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    tart.exists["clawbox-1"] = True
    tart.running["clawbox-1"] = True
    monkeypatch.setattr(orchestrator, "_stop_vm_and_wait", lambda *_args, **_kwargs: False)
    with pytest.raises(UserFacingError, match="Timed out waiting for VM 'clawbox-1' to stop"):
        orchestrator.down_vm(1, tart)


def test_down_vm_already_stopped_message(monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    tart.exists["clawbox-1"] = True
    tart.running["clawbox-1"] = False
    monkeypatch.setattr(orchestrator, "cleanup_locks_for_vm", lambda *_args, **_kwargs: None)
    out = _capture_stdout(lambda: orchestrator.down_vm(1, tart))
    assert "already stopped" in out


def test_delete_vm_nonexistent_cleans_state(isolated_paths, monkeypatch: pytest.MonkeyPatch):
    marker = orchestrator.STATE_DIR / "clawbox-1.provisioned"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("profile: standard\n", encoding="utf-8")
    tart = FakeTart()
    cleaned: list[str] = []
    monkeypatch.setattr(orchestrator, "cleanup_locks_for_vm", lambda vm_name: cleaned.append(vm_name))
    out = _capture_stdout(lambda: orchestrator.delete_vm(1, tart))
    assert "does not exist" in out
    assert not marker.exists()
    assert cleaned == ["clawbox-1"]


def test_delete_vm_timeout_before_delete(monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    tart.exists["clawbox-1"] = True
    tart.running["clawbox-1"] = True
    monkeypatch.setattr(orchestrator, "_stop_vm_and_wait", lambda *_args, **_kwargs: False)
    with pytest.raises(UserFacingError, match="Timed out waiting for VM 'clawbox-1' to stop before deletion"):
        orchestrator.delete_vm(1, tart)


def test_delete_vm_still_exists_after_delete(monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    tart.exists["clawbox-1"] = True
    tart.running["clawbox-1"] = False
    monkeypatch.setattr(orchestrator, "_wait_for_vm_absent", lambda *_args, **_kwargs: False)
    with pytest.raises(UserFacingError, match="still exists after delete attempt"):
        orchestrator.delete_vm(1, tart)


def test_ip_vm_errors_when_ip_unavailable():
    tart = FakeTart()
    tart.exists["clawbox-1"] = True
    tart.running["clawbox-1"] = True
    tart.ip_map["clawbox-1"] = None
    with pytest.raises(UserFacingError, match="Could not resolve IP"):
        orchestrator.ip_vm(1, tart)
