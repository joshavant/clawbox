from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from clawbox import image as image_ops
from clawbox import locks as lock_ops
from clawbox import orchestrator
from clawbox import status as status_ops
from clawbox.locks import OPENCLAW_SOURCE_LOCK, LockError, acquire_path_lock
from clawbox.orchestrator import ProvisionOptions, UpOptions, UserFacingError
from clawbox.tart import TartError


class DummyProcess:
    def __init__(self, pid: int = 1234, poll_value: int | None = None):
        self.pid = pid
        self._poll_value = poll_value

    def poll(self):
        return self._poll_value


class FakeTart:
    def __init__(self):
        self.exists: dict[str, bool] = {}
        self.running: dict[str, bool] = {}
        self.run_calls: list[tuple[str, list[str], Path]] = []
        self.clone_calls: list[tuple[str, str]] = []
        self.stop_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.next_proc = DummyProcess()

    def vm_exists(self, vm_name: str) -> bool:
        return self.exists.get(vm_name, False)

    def vm_running(self, vm_name: str) -> bool:
        return self.running.get(vm_name, False)

    def clone(self, base_image: str, vm_name: str) -> None:
        self.clone_calls.append((base_image, vm_name))
        self.exists[vm_name] = True

    def run_in_background(self, vm_name: str, run_args: list[str], log_file: Path):
        self.run_calls.append((vm_name, run_args, log_file))
        self.running[vm_name] = True
        return self.next_proc

    def stop(self, vm_name: str) -> None:
        self.stop_calls.append(vm_name)
        self.running[vm_name] = False

    def delete(self, vm_name: str) -> None:
        self.delete_calls.append(vm_name)
        self.running[vm_name] = False
        self.exists[vm_name] = False

    def ip(self, vm_name: str) -> str | None:  # pragma: no cover - not used in unit tests
        return "192.168.64.10"


@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(orchestrator, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(orchestrator, "ANSIBLE_DIR", tmp_path / "ansible")
    monkeypatch.setattr(orchestrator, "SECRETS_FILE", tmp_path / "ansible" / "secrets.yml")
    monkeypatch.setattr(orchestrator, "STATE_DIR", tmp_path / ".clawbox" / "state")
    monkeypatch.setattr(orchestrator, "start_vm_watcher", lambda *_args, **_kwargs: 9999)
    monkeypatch.setattr(orchestrator, "stop_vm_watcher", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(orchestrator, "mutagen_available", lambda: True)
    monkeypatch.setattr(orchestrator, "_activate_mutagen_sync", lambda **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_activate_mutagen_sync_from_locks", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_deactivate_mutagen_sync", lambda *_args, **_kwargs: None)
    (tmp_path / "ansible").mkdir(parents=True, exist_ok=True)
    yield tmp_path


def capture_stdout(fn):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    return buf.getvalue()


def test_create_vm_success(isolated_paths):
    tart = FakeTart()
    out = capture_stdout(lambda: orchestrator.create_vm(91, tart))
    assert tart.clone_calls == [(orchestrator.BASE_IMAGE, "clawbox-91")]
    assert "Created VM: clawbox-91" in out


def test_create_vm_rejects_existing(isolated_paths):
    tart = FakeTart()
    tart.exists["clawbox-91"] = True
    with pytest.raises(UserFacingError, match="already exists"):
        orchestrator.create_vm(91, tart)


def test_create_vm_surfaces_virtualization_limit_hint(isolated_paths):
    class FailingTart(FakeTart):
        def clone(self, base_image: str, vm_name: str) -> None:
            raise TartError("Error Domain=VZErrorDomain Code=1")

    tart = FailingTart()
    with pytest.raises(UserFacingError, match="Virtualization.framework may be refusing another VM"):
        orchestrator.create_vm(91, tart)


def test_virtualization_hint_includes_tart_system_limit_phrase():
    message = "The number of VMs exceeds the system limit (other running VMs: clawbox-91, clawbox-92)"
    hinted = orchestrator._with_virtualization_limit_hint(message)
    assert "Virtualization.framework may be refusing another VM" in hinted


def test_launch_vm_headless_passes_no_graphics(isolated_paths, monkeypatch):
    tart = FakeTart()
    tart.exists["clawbox-91"] = True
    monkeypatch.setattr(orchestrator, "_acquire_locks", lambda *args, **kwargs: None)
    out = capture_stdout(
        lambda: orchestrator.launch_vm(
            vm_number=91,
            profile="standard",
            openclaw_source="",
            openclaw_payload="",
            signal_payload="",
            headless=True,
            tart=tart,
        )
    )
    assert "launch mode:          headless" in out
    assert tart.run_calls
    _, run_args, _ = tart.run_calls[0]
    assert "--no-graphics" in run_args


def test_launch_vm_developer_requires_mounts(isolated_paths):
    tart = FakeTart()
    with pytest.raises(UserFacingError, match="requires --openclaw-source and --openclaw-payload"):
        orchestrator.launch_vm(
            vm_number=91,
            profile="developer",
            openclaw_source="",
            openclaw_payload="",
            signal_payload="",
            headless=False,
            tart=tart,
        )


def test_launch_vm_missing_vm_has_no_lock_or_marker_side_effects(isolated_paths, monkeypatch):
    tart = FakeTart()
    lock_calls: list[str] = []
    source_dir = isolated_paths / "source"
    payload_dir = isolated_paths / "payload"
    marker_dir = isolated_paths / "signal"
    source_dir.mkdir(parents=True, exist_ok=True)
    payload_dir.mkdir(parents=True, exist_ok=True)
    marker_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        orchestrator,
        "_acquire_locks",
        lambda *args, **kwargs: lock_calls.append("called"),
    )

    with pytest.raises(UserFacingError, match="does not exist"):
        orchestrator.launch_vm(
            vm_number=91,
            profile="developer",
            openclaw_source=str(source_dir),
            openclaw_payload=str(payload_dir),
            signal_payload=str(marker_dir),
            headless=False,
            tart=tart,
        )

    assert lock_calls == []
    assert list(marker_dir.iterdir()) == []


def test_launch_vm_surfaces_early_tart_exit(isolated_paths, monkeypatch):
    tart = FakeTart()
    tart.exists["clawbox-91"] = True
    tart.next_proc = DummyProcess(pid=4321, poll_value=1)
    monkeypatch.setattr(orchestrator, "_acquire_locks", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "tail_lines", lambda *args, **kwargs: "simulated tart failure")

    with pytest.raises(UserFacingError, match="tart run exited before 'clawbox-91' reached a running state"):
        orchestrator.launch_vm(
            vm_number=91,
            profile="standard",
            openclaw_source="",
            openclaw_payload="",
            signal_payload="",
            headless=False,
            tart=tart,
        )


def test_launch_vm_surfaces_running_timeout(isolated_paths, monkeypatch):
    tart = FakeTart()
    tart.exists["clawbox-91"] = True
    monkeypatch.setattr(orchestrator, "_acquire_locks", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "wait_for_vm_running", lambda *args, **kwargs: False)
    monkeypatch.setattr(orchestrator, "tail_lines", lambda *args, **kwargs: "simulated timeout")

    with pytest.raises(UserFacingError, match="did not enter running state within 30s"):
        orchestrator.launch_vm(
            vm_number=91,
            profile="standard",
            openclaw_source="",
            openclaw_payload="",
            signal_payload="",
            headless=False,
            tart=tart,
        )


def test_launch_vm_running_vm_refreshes_lock_and_marker_work(isolated_paths, monkeypatch):
    tart = FakeTart()
    vm_name = "clawbox-91"
    tart.exists[vm_name] = True
    tart.running[vm_name] = True

    source = isolated_paths / "source"
    payload = isolated_paths / "payload"
    signal = isolated_paths / "signal"
    source.mkdir(parents=True, exist_ok=True)
    payload.mkdir(parents=True, exist_ok=True)
    signal.mkdir(parents=True, exist_ok=True)

    lock_calls: list[str] = []
    marker_calls: list[str] = []
    monkeypatch.setattr(
        orchestrator,
        "_acquire_locks",
        lambda *args, **kwargs: lock_calls.append("called"),
    )
    monkeypatch.setattr(
        orchestrator,
        "_ensure_signal_payload_host_marker",
        lambda *args, **kwargs: marker_calls.append("called"),
    )

    out = capture_stdout(
        lambda: orchestrator.launch_vm(
            vm_number=91,
            profile="developer",
            openclaw_source=str(source),
            openclaw_payload=str(payload),
            signal_payload=str(signal),
            headless=False,
            tart=tart,
        )
    )
    assert "VM 'clawbox-91' is already running." in out
    assert lock_calls == ["called"]
    assert marker_calls == ["called"]


def test_up_standard_rejects_developer_flags(isolated_paths):
    tart = FakeTart()
    with pytest.raises(UserFacingError, match="only valid in developer mode"):
        orchestrator.up(
            UpOptions(
                vm_number=91,
                profile="standard",
                openclaw_source="/tmp/src",
                openclaw_payload="/tmp/payload",
                signal_payload="",
                enable_playwright=False,
                enable_tailscale=False,
                enable_signal_cli=False,
            ),
            tart,
        )


def test_up_first_run_uses_headless_then_gui(isolated_paths, monkeypatch):
    tart = FakeTart()
    calls: list[str] = []
    orchestrator.ensure_secrets_file(create_if_missing=True)

    def fake_create(vm_number, _tart):
        calls.append(f"create:{vm_number}")
        _tart.exists["clawbox-91"] = True

    def fake_launch(
        vm_number,
        profile,
        openclaw_source,
        openclaw_payload,
        signal_payload,
        headless,
        tart,
    ):
        calls.append(f"launch:headless={str(headless).lower()}")
        tart.running["clawbox-91"] = True

    def fake_provision(opts, _tart):
        calls.append("provision")
        marker = orchestrator.ProvisionMarker(
            vm_name="clawbox-91",
            profile=opts.profile,
            playwright=opts.enable_playwright,
            tailscale=opts.enable_tailscale,
            signal_cli=opts.enable_signal_cli,
            signal_payload=opts.enable_signal_payload,
            provisioned_at="2026-01-01T00:00:00Z",
        )
        marker.write(orchestrator.STATE_DIR / "clawbox-91.provisioned")

    monkeypatch.setattr(orchestrator, "_acquire_locks", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "create_vm", fake_create)
    monkeypatch.setattr(orchestrator, "launch_vm", fake_launch)
    monkeypatch.setattr(orchestrator, "provision_vm", fake_provision)
    monkeypatch.setattr(orchestrator, "wait_for_vm_running", lambda *args, **kwargs: True)

    out = capture_stdout(
        lambda: orchestrator.up(
            UpOptions(
                vm_number=91,
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
    )

    assert calls == ["create:91", "launch:headless=true", "provision", "launch:headless=false"]
    assert "Clawbox is ready: clawbox-91" in out
    assert "VM window may appear before host<->VM sync is ready." not in out
    assert "Wait for 'Clawbox is ready:' before logging in or editing synced files." not in out


def test_up_first_run_developer_includes_sync_readiness_note(isolated_paths, monkeypatch):
    tart = FakeTart()
    calls: list[str] = []
    orchestrator.ensure_secrets_file(create_if_missing=True)

    def fake_create(vm_number, _tart):
        calls.append(f"create:{vm_number}")
        _tart.exists["clawbox-91"] = True

    def fake_launch(
        vm_number,
        profile,
        openclaw_source,
        openclaw_payload,
        signal_payload,
        headless,
        tart,
    ):
        calls.append(f"launch:headless={str(headless).lower()}")
        tart.running["clawbox-91"] = True

    def fake_provision(opts, _tart):
        calls.append("provision")
        marker = orchestrator.ProvisionMarker(
            vm_name="clawbox-91",
            profile=opts.profile,
            playwright=opts.enable_playwright,
            tailscale=opts.enable_tailscale,
            signal_cli=opts.enable_signal_cli,
            signal_payload=opts.enable_signal_payload,
            provisioned_at="2026-01-01T00:00:00Z",
            sync_backend="mutagen",
        )
        marker.write(orchestrator.STATE_DIR / "clawbox-91.provisioned")

    monkeypatch.setattr(orchestrator, "_acquire_locks", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_preflight_developer_mounts", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "create_vm", fake_create)
    monkeypatch.setattr(orchestrator, "launch_vm", fake_launch)
    monkeypatch.setattr(orchestrator, "provision_vm", fake_provision)
    monkeypatch.setattr(orchestrator, "wait_for_vm_running", lambda *args, **kwargs: True)

    out = capture_stdout(
        lambda: orchestrator.up(
            UpOptions(
                vm_number=91,
                profile="developer",
                openclaw_source=str(isolated_paths),
                openclaw_payload=str(isolated_paths),
                signal_payload="",
                enable_playwright=False,
                enable_tailscale=False,
                enable_signal_cli=False,
            ),
            tart,
        )
    )

    assert calls == ["create:91", "launch:headless=true", "provision", "launch:headless=false"]
    assert "Clawbox is ready: clawbox-91" in out
    assert "VM window may appear before host<->VM sync is ready." in out
    assert "Wait for 'Clawbox is ready:' before logging in or editing synced files." in out


def test_up_marker_match_skips_provision(isolated_paths, monkeypatch):
    tart = FakeTart()
    vm_name = "clawbox-91"
    tart.exists[vm_name] = True
    tart.running[vm_name] = True
    orchestrator.ensure_secrets_file(create_if_missing=True)
    orchestrator.ProvisionMarker(
        vm_name=vm_name,
        profile="standard",
        playwright=False,
        tailscale=False,
        signal_cli=False,
        signal_payload=False,
        provisioned_at="2026-01-01T00:00:00Z",
    ).write(orchestrator.STATE_DIR / f"{vm_name}.provisioned")
    monkeypatch.setattr(orchestrator, "_acquire_locks", lambda *args, **kwargs: None)

    out = capture_stdout(
        lambda: orchestrator.up(
            UpOptions(
                vm_number=91,
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
    )
    assert "Provision marker found for 'clawbox-91'; skipping provisioning." in out
    assert "Clawbox is running: clawbox-91 (provisioning skipped)" in out


def test_up_marker_match_running_vm_does_not_reacquire_locks(isolated_paths, monkeypatch):
    tart = FakeTart()
    vm_name = "clawbox-91"
    tart.exists[vm_name] = True
    tart.running[vm_name] = True
    orchestrator.ensure_secrets_file(create_if_missing=True)
    orchestrator.ProvisionMarker(
        vm_name=vm_name,
        profile="standard",
        playwright=False,
        tailscale=False,
        signal_cli=False,
        signal_payload=False,
        provisioned_at="2026-01-01T00:00:00Z",
    ).write(orchestrator.STATE_DIR / f"{vm_name}.provisioned")

    monkeypatch.setattr(
        orchestrator,
        "_acquire_locks",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not lock")),
    )

    out = capture_stdout(
        lambda: orchestrator.up(
            UpOptions(
                vm_number=91,
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
    )
    assert "Clawbox is running: clawbox-91 (provisioning skipped)" in out


def test_recreate_runs_down_delete_then_up(isolated_paths, monkeypatch):
    tart = FakeTart()
    tart.exists["clawbox-93"] = True
    calls: list[str] = []

    monkeypatch.setattr(
        orchestrator,
        "down_vm",
        lambda vm_number, _tart: calls.append(f"down:{vm_number}"),
    )
    monkeypatch.setattr(
        orchestrator,
        "delete_vm",
        lambda vm_number, _tart: calls.append(f"delete:{vm_number}"),
    )
    monkeypatch.setattr(
        orchestrator,
        "up",
        lambda opts, _tart: calls.append(
            f"up:{opts.vm_number}:{opts.profile}:{str(opts.enable_playwright).lower()}"
        ),
    )

    out = capture_stdout(
        lambda: orchestrator.recreate(
            UpOptions(
                vm_number=93,
                profile="developer",
                openclaw_source=str(isolated_paths),
                openclaw_payload=str(isolated_paths),
                signal_payload="",
                enable_playwright=True,
                enable_tailscale=False,
                enable_signal_cli=False,
            ),
            tart,
        )
    )

    assert "Clean recreate requested for 'clawbox-93'." in out
    assert calls == ["down:93", "delete:93", "up:93:developer:true"]


def test_recreate_missing_vm_runs_delete_then_up(isolated_paths, monkeypatch):
    tart = FakeTart()
    calls: list[str] = []

    monkeypatch.setattr(
        orchestrator,
        "down_vm",
        lambda vm_number, _tart: calls.append(f"down:{vm_number}"),
    )
    monkeypatch.setattr(
        orchestrator,
        "delete_vm",
        lambda vm_number, _tart: calls.append(f"delete:{vm_number}"),
    )
    monkeypatch.setattr(
        orchestrator,
        "up",
        lambda opts, _tart: calls.append(f"up:{opts.vm_number}:{opts.profile}"),
    )

    orchestrator.recreate(
        UpOptions(
            vm_number=94,
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

    assert calls == ["delete:94", "up:94:standard"]


def test_up_missing_marker_on_existing_vm_requires_recreate(isolated_paths, monkeypatch):
    tart = FakeTart()
    vm_name = "clawbox-91"
    tart.exists[vm_name] = True
    tart.running[vm_name] = True
    orchestrator.ensure_secrets_file(create_if_missing=True)
    monkeypatch.setattr(orchestrator, "_acquire_locks", lambda *args, **kwargs: None)

    with pytest.raises(UserFacingError, match="Provision marker is missing for existing VM"):
        orchestrator.up(
            UpOptions(
                vm_number=91,
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


def test_up_marker_mismatch_requires_recreate(isolated_paths, monkeypatch):
    tart = FakeTart()
    vm_name = "clawbox-91"
    tart.exists[vm_name] = True
    tart.running[vm_name] = True
    orchestrator.ensure_secrets_file(create_if_missing=True)
    orchestrator.ProvisionMarker(
        vm_name=vm_name,
        profile="standard",
        playwright=False,
        tailscale=False,
        signal_cli=False,
        signal_payload=False,
        provisioned_at="2026-01-01T00:00:00Z",
    ).write(orchestrator.STATE_DIR / f"{vm_name}.provisioned")

    monkeypatch.setattr(orchestrator, "_acquire_locks", lambda *args, **kwargs: None)

    with pytest.raises(UserFacingError, match="Requested options do not match"):
        orchestrator.up(
            UpOptions(
                vm_number=91,
                profile="developer",
                openclaw_source=str(isolated_paths),
                openclaw_payload=str(isolated_paths),
                signal_payload="",
                enable_playwright=True,
                enable_tailscale=False,
                enable_signal_cli=False,
            ),
            tart,
        )


def test_up_developer_marker_missing_sync_backend_requires_recreate(isolated_paths, monkeypatch):
    tart = FakeTart()
    vm_name = "clawbox-91"
    tart.exists[vm_name] = True
    tart.running[vm_name] = True
    orchestrator.ensure_secrets_file(create_if_missing=True)
    orchestrator.ProvisionMarker(
        vm_name=vm_name,
        profile="developer",
        playwright=False,
        tailscale=False,
        signal_cli=False,
        signal_payload=False,
        provisioned_at="2026-01-01T00:00:00Z",
        sync_backend="",
    ).write(orchestrator.STATE_DIR / f"{vm_name}.provisioned")
    monkeypatch.setattr(orchestrator, "_acquire_locks", lambda *args, **kwargs: None)

    with pytest.raises(UserFacingError, match="legacy provision marker format"):
        orchestrator.up(
            UpOptions(
                vm_number=91,
                profile="developer",
                openclaw_source=str(isolated_paths),
                openclaw_payload=str(isolated_paths),
                signal_payload="",
                enable_playwright=False,
                enable_tailscale=False,
                enable_signal_cli=False,
            ),
            tart,
        )


def test_provision_standard_accepts_optional_flags(isolated_paths, monkeypatch):
    tart = FakeTart()
    tart.exists["clawbox-91"] = True
    tart.running["clawbox-91"] = True
    orchestrator.ensure_secrets_file(create_if_missing=True)
    monkeypatch.setattr(orchestrator, "_resolve_vm_ip", lambda *args, **kwargs: "192.168.64.10")
    seen_playbook_cmd: list[str] = []

    def fake_run(args, **kwargs):
        if args and args[0] == "ansible-playbook":
            seen_playbook_cmd[:] = list(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)
    orchestrator.provision_vm(
        ProvisionOptions(
            vm_number=91,
            profile="standard",
            enable_playwright=True,
            enable_tailscale=True,
            enable_signal_cli=True,
            enable_signal_payload=False,
        ),
        tart,
    )
    assert "clawbox_enable_playwright=true" in seen_playbook_cmd
    assert "clawbox_enable_tailscale=true" in seen_playbook_cmd
    assert "clawbox_enable_signal_cli=true" in seen_playbook_cmd


def test_provision_developer_writes_sync_backend_mutagen(isolated_paths, monkeypatch):
    tart = FakeTart()
    vm_name = "clawbox-91"
    tart.exists[vm_name] = True
    tart.running[vm_name] = True
    marker_file = orchestrator.STATE_DIR / f"{vm_name}.provisioned"
    marker_file.unlink(missing_ok=True)
    orchestrator.ensure_secrets_file(create_if_missing=True)
    monkeypatch.setattr(orchestrator, "_resolve_vm_ip", lambda *args, **kwargs: "192.168.64.10")
    monkeypatch.setattr(orchestrator, "_activate_mutagen_sync_from_locks", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orchestrator.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr=""),
    )

    orchestrator.provision_vm(
        ProvisionOptions(
            vm_number=91,
            profile="developer",
            enable_playwright=False,
            enable_tailscale=False,
            enable_signal_cli=False,
            enable_signal_payload=False,
        ),
        tart,
    )
    marker = orchestrator.ProvisionMarker.from_file(marker_file)
    assert marker is not None
    assert marker.sync_backend == "mutagen"


def test_provision_developer_activates_sync_from_locks_by_default(isolated_paths, monkeypatch):
    tart = FakeTart()
    vm_name = "clawbox-91"
    tart.exists[vm_name] = True
    tart.running[vm_name] = True
    orchestrator.ensure_secrets_file(create_if_missing=True)
    monkeypatch.setattr(orchestrator, "_resolve_vm_ip", lambda *args, **kwargs: "192.168.64.10")

    activations: list[str] = []
    monkeypatch.setattr(
        orchestrator,
        "_activate_mutagen_sync_from_locks",
        lambda _vm_name, _tart, **_kwargs: activations.append(_vm_name),
    )
    monkeypatch.setattr(
        orchestrator.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr=""),
    )

    orchestrator.provision_vm(
        ProvisionOptions(
            vm_number=91,
            profile="developer",
            enable_playwright=False,
            enable_tailscale=False,
            enable_signal_cli=False,
            enable_signal_payload=False,
        ),
        tart,
    )

    assert activations == [vm_name]


def test_provision_developer_can_skip_sync_reactivation(isolated_paths, monkeypatch):
    tart = FakeTart()
    vm_name = "clawbox-91"
    tart.exists[vm_name] = True
    tart.running[vm_name] = True
    orchestrator.ensure_secrets_file(create_if_missing=True)
    monkeypatch.setattr(orchestrator, "_resolve_vm_ip", lambda *args, **kwargs: "192.168.64.10")
    monkeypatch.setattr(
        orchestrator,
        "_activate_mutagen_sync_from_locks",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("should skip mutagen reactivation")
        ),
    )
    monkeypatch.setattr(
        orchestrator.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr=""),
    )

    orchestrator.provision_vm(
        ProvisionOptions(
            vm_number=91,
            profile="developer",
            enable_playwright=False,
            enable_tailscale=False,
            enable_signal_cli=False,
            enable_signal_payload=False,
            skip_sync_activation=True,
        ),
        tart,
    )


def test_up_signal_payload_requires_explicit_signal_cli_flag(isolated_paths):
    tart = FakeTart()
    with pytest.raises(UserFacingError, match="--signal-cli-payload requires --add-signal-cli-provisioning"):
        orchestrator.up(
            UpOptions(
                vm_number=91,
                profile="developer",
                openclaw_source=str(isolated_paths),
                openclaw_payload=str(isolated_paths),
                signal_payload=str(isolated_paths),
                enable_playwright=False,
                enable_tailscale=False,
                enable_signal_cli=False,
            ),
            tart,
        )


def test_provision_signal_payload_requires_explicit_signal_cli_flag(isolated_paths):
    tart = FakeTart()
    with pytest.raises(
        UserFacingError, match="--enable-signal-payload requires --add-signal-cli-provisioning"
    ):
        orchestrator.provision_vm(
            ProvisionOptions(
                vm_number=91,
                profile="developer",
                enable_playwright=False,
                enable_tailscale=False,
                enable_signal_cli=False,
                enable_signal_payload=True,
            ),
            tart,
        )


def test_provision_vm_surfaces_playbook_failure(isolated_paths, monkeypatch):
    tart = FakeTart()
    tart.exists["clawbox-91"] = True
    tart.running["clawbox-91"] = True
    orchestrator.ensure_secrets_file(create_if_missing=True)
    monkeypatch.setattr(orchestrator, "_resolve_vm_ip", lambda *args, **kwargs: "192.168.64.10")
    seen_playbook_cmd: list[str] = []

    def fake_run(args, **kwargs):
        if args and args[0] == "ansible-playbook":
            seen_playbook_cmd[:] = list(args)
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)
    with pytest.raises(UserFacingError, match="Provisioning failed"):
        orchestrator.provision_vm(
            ProvisionOptions(
                vm_number=91,
                profile="standard",
                enable_playwright=False,
                enable_tailscale=False,
                enable_signal_cli=False,
                enable_signal_payload=False,
            ),
            tart,
        )
    assert "-i" in seen_playbook_cmd
    inventory_index = seen_playbook_cmd.index("-i")
    assert seen_playbook_cmd[inventory_index + 1] == "192.168.64.10,"
    assert "ansible_become=true" in seen_playbook_cmd


def test_preflight_signal_payload_marker_times_out(isolated_paths, monkeypatch):
    marker_path = (
        f"{orchestrator.SIGNAL_PAYLOAD_MOUNT}/{orchestrator.SIGNAL_PAYLOAD_MARKER_FILENAME}"
    )
    monkeypatch.setattr(
        orchestrator,
        "_ansible_shell",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=["ansible"],
            returncode=1,
            stdout=f"{marker_path}=missing\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(orchestrator.time, "sleep", lambda *args, **kwargs: None)

    with pytest.raises(UserFacingError, match="signal-cli payload marker was not visible"):
        orchestrator._preflight_signal_payload_marker(
            "clawbox-91",
            vm_number=91,
            timeout_seconds=2,
        )


def test_provision_vm_developer_signal_payload_runs_marker_preflight(isolated_paths, monkeypatch):
    tart = FakeTart()
    tart.exists["clawbox-91"] = True
    tart.running["clawbox-91"] = True
    orchestrator.ensure_secrets_file(create_if_missing=True)
    called: list[dict[str, object]] = []
    playbook_calls: list[list[str]] = []

    monkeypatch.setattr(orchestrator, "_resolve_vm_ip", lambda *args, **kwargs: "192.168.64.10")
    monkeypatch.setattr(
        orchestrator,
        "_preflight_signal_payload_marker",
        lambda vm_name, **kwargs: called.append({"vm_name": vm_name, **kwargs}),
    )

    def fake_run(args, **kwargs):
        if args and args[0] == "ansible-playbook":
            playbook_calls.append(list(args))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)

    orchestrator.provision_vm(
        ProvisionOptions(
            vm_number=91,
            profile="developer",
            enable_playwright=False,
            enable_tailscale=False,
            enable_signal_cli=True,
            enable_signal_payload=True,
        ),
        tart,
    )

    assert called == [
        {
            "vm_name": "clawbox-91",
            "vm_number": 91,
            "timeout_seconds": 120,
            "inventory_path": "192.168.64.10,",
            "target_host": "192.168.64.10",
        }
    ]
    assert playbook_calls
    playbook_cmd = playbook_calls[0]
    assert "-i" in playbook_cmd
    inventory_index = playbook_cmd.index("-i")
    assert playbook_cmd[inventory_index + 1] == "192.168.64.10,"
    assert "vm_number=91" in playbook_cmd


def test_provision_vm_fails_when_vm_missing(isolated_paths):
    tart = FakeTart()
    orchestrator.ensure_secrets_file(create_if_missing=True)

    with pytest.raises(UserFacingError, match="does not exist"):
        orchestrator.provision_vm(
            ProvisionOptions(
                vm_number=91,
                profile="standard",
                enable_playwright=False,
                enable_tailscale=False,
                enable_signal_cli=False,
                enable_signal_payload=False,
            ),
            tart,
        )


def test_provision_vm_fails_when_vm_not_running(isolated_paths):
    tart = FakeTart()
    tart.exists["clawbox-91"] = True
    tart.running["clawbox-91"] = False
    orchestrator.ensure_secrets_file(create_if_missing=True)

    with pytest.raises(UserFacingError, match="is not running"):
        orchestrator.provision_vm(
            ProvisionOptions(
                vm_number=91,
                profile="standard",
                enable_playwright=False,
                enable_tailscale=False,
                enable_signal_cli=False,
                enable_signal_payload=False,
            ),
            tart,
        )


def test_up_developer_runs_mount_preflight(isolated_paths, monkeypatch):
    tart = FakeTart()
    called: list[dict[str, object]] = []

    def fake_create(vm_number, _tart):
        _tart.exists["clawbox-91"] = True

    def fake_launch(
        vm_number,
        profile,
        openclaw_source,
        openclaw_payload,
        signal_payload,
        headless,
        tart,
    ):
        tart.running["clawbox-91"] = True

    def fake_provision(opts, _tart):
        marker = orchestrator.ProvisionMarker(
            vm_name="clawbox-91",
            profile=opts.profile,
            playwright=opts.enable_playwright,
            tailscale=opts.enable_tailscale,
            signal_cli=opts.enable_signal_cli,
            signal_payload=opts.enable_signal_payload,
            provisioned_at="2026-01-01T00:00:00Z",
        )
        marker.write(orchestrator.STATE_DIR / "clawbox-91.provisioned")

    monkeypatch.setattr(orchestrator, "_acquire_locks", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "create_vm", fake_create)
    monkeypatch.setattr(orchestrator, "launch_vm", fake_launch)
    monkeypatch.setattr(orchestrator, "provision_vm", fake_provision)
    monkeypatch.setattr(orchestrator, "wait_for_vm_running", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        orchestrator,
        "_preflight_developer_mounts",
        lambda vm_name, **kwargs: called.append({"vm_name": vm_name, **kwargs}),
    )

    orchestrator.up(
        UpOptions(
            vm_number=91,
            profile="developer",
            openclaw_source=str(isolated_paths),
            openclaw_payload=str(isolated_paths),
            signal_payload="",
            enable_playwright=False,
            enable_tailscale=False,
            enable_signal_cli=False,
        ),
        tart,
    )

    assert called == [
        {
            "vm_name": "clawbox-91",
            "vm_number": 91,
            "openclaw_payload_host": str(isolated_paths),
            "signal_payload_host": "",
            "include_signal_payload": False,
            "timeout_seconds": 120,
        }
    ]


def test_preflight_developer_mounts_cleans_probe_files_on_error(tmp_path: Path, monkeypatch):
    payload_dir = tmp_path / "openclaw-payload"
    signal_dir = tmp_path / "signal-payload"
    payload_dir.mkdir(parents=True, exist_ok=True)
    signal_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(orchestrator, "OPENCLAW_SOURCE_MOUNT", "/Users/Shared/clawbox-sync/openclaw-source")
    monkeypatch.setattr(orchestrator, "OPENCLAW_PAYLOAD_MOUNT", "/Users/Shared/clawbox-sync/openclaw-payload")
    monkeypatch.setattr(orchestrator, "SIGNAL_PAYLOAD_MOUNT", "/Users/Shared/clawbox-sync/signal-cli-payload")
    monkeypatch.setattr(orchestrator, "_ansible_shell", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("probe failed")))

    with pytest.raises(RuntimeError, match="probe failed"):
        orchestrator._preflight_developer_mounts(
            "clawbox-91",
            vm_number=91,
            openclaw_payload_host=str(payload_dir),
            signal_payload_host=str(signal_dir),
            include_signal_payload=True,
            timeout_seconds=5,
        )

    assert list(payload_dir.iterdir()) == []
    assert list(signal_dir.iterdir()) == []


def test_resolve_vm_ip_times_out(monkeypatch):
    tart = FakeTart()
    tart.ip = lambda vm_name: None  # type: ignore[method-assign]
    monkeypatch.setattr(orchestrator.time, "sleep", lambda *args, **kwargs: None)
    with pytest.raises(UserFacingError, match="Timed out waiting for 'clawbox-91' to report an IP address"):
        orchestrator._resolve_vm_ip(tart, "clawbox-91", timeout_seconds=1)


def test_openclaw_source_lock_conflict_and_reclaim(tmp_path: Path):
    tart = FakeTart()
    path = tmp_path / "shared-source"
    vm1 = "clawbox-91"
    vm2 = "clawbox-92"
    tart.running[vm1] = True

    old_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = str(tmp_path / "home")
        Path(os.environ["HOME"]).mkdir(parents=True, exist_ok=True)

        acquire_path_lock(OPENCLAW_SOURCE_LOCK, vm1, str(path), tart)
        with pytest.raises(LockError, match="already in use by running VM 'clawbox-91'"):
            acquire_path_lock(OPENCLAW_SOURCE_LOCK, vm2, str(path), tart)

        tart.running[vm1] = False
        acquire_path_lock(OPENCLAW_SOURCE_LOCK, vm2, str(path), tart)

        lock_root = Path(os.environ["HOME"]) / ".clawbox" / "locks" / OPENCLAW_SOURCE_LOCK.lock_kind
        owner_vm_files = list(lock_root.rglob("owner_vm"))
        assert owner_vm_files
        assert "clawbox-92" in owner_vm_files[0].read_text(encoding="utf-8")
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home


def test_openclaw_source_lock_reclaims_corrupt_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    path = tmp_path / "shared-source"
    vm_name = "clawbox-91"
    home = tmp_path / "home"
    old_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = str(home)
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(lock_ops.time, "sleep", lambda *_args, **_kwargs: None)

        canonical = path.expanduser().resolve()
        key = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()
        lock_dir = home / ".clawbox" / "locks" / OPENCLAW_SOURCE_LOCK.lock_kind / key
        lock_dir.mkdir(parents=True, exist_ok=True)

        acquire_path_lock(OPENCLAW_SOURCE_LOCK, vm_name, str(path), tart)

        owner_vm = (lock_dir / "owner_vm").read_text(encoding="utf-8").strip()
        assert owner_vm == vm_name
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home


def test_openclaw_source_lock_prunes_previous_lock_for_same_vm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    tart = FakeTart()
    path_one = tmp_path / "shared-source-1"
    path_two = tmp_path / "shared-source-2"
    vm_name = "clawbox-91"
    home = tmp_path / "home"
    old_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = str(home)
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(lock_ops.time, "sleep", lambda *_args, **_kwargs: None)

        acquire_path_lock(OPENCLAW_SOURCE_LOCK, vm_name, str(path_one), tart)
        acquire_path_lock(OPENCLAW_SOURCE_LOCK, vm_name, str(path_two), tart)

        lock_root = home / ".clawbox" / "locks" / OPENCLAW_SOURCE_LOCK.lock_kind
        lock_dirs = [d for d in lock_root.iterdir() if d.is_dir()]
        assert len(lock_dirs) == 1
        assert (lock_dirs[0] / "owner_vm").read_text(encoding="utf-8").strip() == vm_name
        assert (lock_dirs[0] / "source_path").read_text(encoding="utf-8").strip() == str(
            path_two.resolve()
        )
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home


def test_down_vm_stops_running_and_cleans_locks(isolated_paths, monkeypatch):
    tart = FakeTart()
    vm_name = "clawbox-91"
    tart.exists[vm_name] = True
    tart.running[vm_name] = True
    cleaned: list[str] = []
    monkeypatch.setattr(orchestrator, "cleanup_locks_for_vm", lambda name: cleaned.append(name))

    out = capture_stdout(lambda: orchestrator.down_vm(91, tart))

    assert vm_name in tart.stop_calls
    assert cleaned == [vm_name]
    assert "VM 'clawbox-91' stopped." in out


def test_delete_vm_removes_vm_marker_and_locks(isolated_paths, monkeypatch):
    tart = FakeTart()
    vm_name = "clawbox-91"
    marker_file = orchestrator.STATE_DIR / f"{vm_name}.provisioned"
    marker_file.parent.mkdir(parents=True, exist_ok=True)
    marker_file.write_text("profile: standard\n", encoding="utf-8")
    tart.exists[vm_name] = True
    tart.running[vm_name] = True
    cleaned: list[str] = []
    monkeypatch.setattr(orchestrator, "cleanup_locks_for_vm", lambda name: cleaned.append(name))

    out = capture_stdout(lambda: orchestrator.delete_vm(91, tart))

    assert vm_name in tart.stop_calls
    assert vm_name in tart.delete_calls
    assert not marker_file.exists()
    assert cleaned == [vm_name]
    assert "Deleted VM: clawbox-91" in out


def test_ip_vm_prints_resolved_ip(isolated_paths):
    tart = FakeTart()
    vm_name = "clawbox-91"
    tart.exists[vm_name] = True
    tart.running[vm_name] = True

    out = capture_stdout(lambda: orchestrator.ip_vm(91, tart))
    assert out.strip() == "192.168.64.10"


def test_ip_vm_fails_when_vm_not_running(isolated_paths):
    tart = FakeTart()
    vm_name = "clawbox-91"
    tart.exists[vm_name] = True
    tart.running[vm_name] = False

    with pytest.raises(UserFacingError, match="is not running"):
        orchestrator.ip_vm(91, tart)


def test_parse_mount_statuses_handles_wrapped_output():
    mount_paths = [
        "/Users/Shared/clawbox-sync/openclaw-source",
        "/Users/Shared/clawbox-sync/openclaw-payload",
    ]
    stdout = "\n".join(
        [
            "clawbox-92 | CHANGED | rc=0 >>",
            "\t/Users/Shared/clawbox-sync/openclaw-source=mounted",
            "prefix text /Users/Shared/clawbox-sync/openclaw-payload=dir suffix text",
        ]
    )

    parsed = orchestrator._parse_mount_statuses(stdout, mount_paths)
    assert parsed["/Users/Shared/clawbox-sync/openclaw-source"] == "mounted"
    assert parsed["/Users/Shared/clawbox-sync/openclaw-payload"] == "dir"


def test_status_vm_reports_mounts_without_signal_daemon_probe(isolated_paths, monkeypatch):
    tart = FakeTart()
    vm_name = "clawbox-91"
    tart.exists[vm_name] = True
    tart.running[vm_name] = True
    marker = orchestrator.ProvisionMarker(
        vm_name=vm_name,
        profile="developer",
        playwright=False,
        tailscale=False,
        signal_cli=True,
        signal_payload=True,
        provisioned_at="2026-01-01T00:00:00Z",
    )
    marker.write(orchestrator.STATE_DIR / f"{vm_name}.provisioned")
    orchestrator.ensure_secrets_file(create_if_missing=True)

    mount_stdout = "\n".join(
        [
            f"{orchestrator.OPENCLAW_SOURCE_MOUNT}=mounted",
            f"{orchestrator.OPENCLAW_PAYLOAD_MOUNT}=mounted",
            f"{orchestrator.SIGNAL_PAYLOAD_MOUNT}=dir",
        ]
    )
    responses = [subprocess.CompletedProcess(args=["ansible"], returncode=0, stdout=mount_stdout, stderr="")]

    def fake_ansible_shell(*args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(status_ops, "_ansible_shell", fake_ansible_shell)
    monkeypatch.setattr(
        status_ops,
        "_probe_mutagen_sync",
        lambda _vm_name: ("ok", True, ["Name: clawbox-clawbox-91-openclaw-source", "Status: Watching for changes"]),
    )

    out = capture_stdout(lambda: orchestrator.status_vm(91, tart))
    assert "sync paths:" in out
    assert f"{orchestrator.SIGNAL_PAYLOAD_MOUNT}: dir" in out
    assert "signal payload sync daemon:" not in out


def test_status_vm_json_reports_mounts_without_signal_daemon_probe(isolated_paths, monkeypatch):
    tart = FakeTart()
    vm_name = "clawbox-91"
    tart.exists[vm_name] = True
    tart.running[vm_name] = True
    marker = orchestrator.ProvisionMarker(
        vm_name=vm_name,
        profile="developer",
        playwright=True,
        tailscale=True,
        signal_cli=True,
        signal_payload=True,
        provisioned_at="2026-01-01T00:00:00Z",
    )
    marker.write(orchestrator.STATE_DIR / f"{vm_name}.provisioned")
    orchestrator.ensure_secrets_file(create_if_missing=True)

    mount_stdout = "\n".join(
        [
            f"{orchestrator.OPENCLAW_SOURCE_MOUNT}=mounted",
            f"{orchestrator.OPENCLAW_PAYLOAD_MOUNT}=mounted",
            f"{orchestrator.SIGNAL_PAYLOAD_MOUNT}=mounted",
        ]
    )
    responses = [subprocess.CompletedProcess(args=["ansible"], returncode=0, stdout=mount_stdout, stderr="")]

    def fake_ansible_shell(*args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(status_ops, "_ansible_shell", fake_ansible_shell)
    monkeypatch.setattr(
        status_ops,
        "_probe_mutagen_sync",
        lambda _vm_name: ("ok", True, ["Name: clawbox-clawbox-91-openclaw-source", "Status: Watching for changes"]),
    )

    out = capture_stdout(lambda: orchestrator.status_vm(91, tart, as_json=True))
    parsed = json.loads(out)
    assert parsed["vm"] == vm_name
    assert parsed["exists"] is True
    assert parsed["running"] is True
    assert parsed["provision_marker"]["present"] is True
    assert parsed["provision_marker"]["data"]["profile"] == "developer"
    assert parsed["sync_paths"]["probe"] == "ok"
    assert parsed["sync_paths"]["paths"][orchestrator.SIGNAL_PAYLOAD_MOUNT] == "mounted"
    assert parsed["signal_payload_sync"]["enabled"] is True
    assert parsed["signal_payload_sync"]["probe"] == "not_applicable"
    assert parsed["signal_payload_sync"]["lines"] == []


def test_status_vm_skips_remote_probe_when_marker_missing(isolated_paths, monkeypatch):
    tart = FakeTart()
    vm_name = "clawbox-91"
    tart.exists[vm_name] = True
    tart.running[vm_name] = True
    orchestrator.SECRETS_FILE.write_text("not_vm_password: nope\n", encoding="utf-8")

    probes = {"count": 0}
    monkeypatch.setattr(
        status_ops,
        "_ansible_shell",
        lambda *args, **kwargs: probes.__setitem__("count", probes["count"] + 1)
        or subprocess.CompletedProcess(args=["ansible"], returncode=0, stdout="", stderr=""),
    )

    out = capture_stdout(lambda: orchestrator.status_vm(91, tart))
    assert probes["count"] == 0
    assert "note: no marker found; skipping remote sync-path probe" in out
    assert "warnings:" not in out


def test_status_vm_json_skips_remote_probe_when_marker_missing(isolated_paths, monkeypatch):
    tart = FakeTart()
    vm_name = "clawbox-91"
    tart.exists[vm_name] = True
    tart.running[vm_name] = True
    orchestrator.SECRETS_FILE.write_text("not_vm_password: nope\n", encoding="utf-8")

    probes = {"count": 0}
    monkeypatch.setattr(
        status_ops,
        "_ansible_shell",
        lambda *args, **kwargs: probes.__setitem__("count", probes["count"] + 1)
        or subprocess.CompletedProcess(args=["ansible"], returncode=0, stdout="", stderr=""),
    )

    out = capture_stdout(lambda: orchestrator.status_vm(91, tart, as_json=True))
    parsed = json.loads(out)
    assert probes["count"] == 0
    assert parsed["sync_paths"]["probe"] == "not_applicable"
    assert parsed["sync_paths"]["note"] == "no marker found; skipping remote sync-path probe"
    assert parsed["warnings"] == []


def test_image_build_runs_init_then_build(isolated_paths, monkeypatch):
    template = isolated_paths / "packer" / "macos-base.pkr.hcl"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text("", encoding="utf-8")
    monkeypatch.setattr(image_ops, "PROJECT_DIR", isolated_paths)
    monkeypatch.setattr(image_ops, "PACKER_TEMPLATE", template)

    calls: list[list[str]] = []

    def fake_run(cmd, cwd, check):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(image_ops.subprocess, "run", fake_run)

    orchestrator.image_build(skip_init=False, force=False)

    assert calls == [
        ["packer", "init", "packer/macos-base.pkr.hcl"],
        ["packer", "build", "packer/macos-base.pkr.hcl"],
    ]


def test_image_rebuild_uses_force(isolated_paths, monkeypatch):
    template = isolated_paths / "packer" / "macos-base.pkr.hcl"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text("", encoding="utf-8")
    monkeypatch.setattr(image_ops, "PROJECT_DIR", isolated_paths)
    monkeypatch.setattr(image_ops, "PACKER_TEMPLATE", template)

    calls: list[list[str]] = []

    def fake_run(cmd, cwd, check):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(image_ops.subprocess, "run", fake_run)

    orchestrator.image_build(skip_init=False, force=True)

    assert calls == [
        ["packer", "init", "packer/macos-base.pkr.hcl"],
        ["packer", "build", "-force", "packer/macos-base.pkr.hcl"],
    ]
