from __future__ import annotations

import io
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from clawbox import status as status_ops
from clawbox.state import ProvisionMarker


class FakeTart:
    def __init__(self, vms: list[dict[str, object]]):
        self.vms = vms

    def list_vms_json(self) -> list[dict[str, object]]:
        return self.vms

    def vm_exists(self, vm_name: str) -> bool:
        return any(vm.get("Name") == vm_name for vm in self.vms)

    def vm_running(self, vm_name: str) -> bool:
        for vm in self.vms:
            if vm.get("Name") == vm_name:
                running = vm.get("Running")
                return bool(running) if isinstance(running, bool) else False
        return False

    def ip(self, vm_name: str) -> str | None:
        for vm in self.vms:
            if vm.get("Name") == vm_name and self.vm_running(vm_name):
                ip = vm.get("IP")
                return ip if isinstance(ip, str) else None
        return None


def _context(tmp_path: Path) -> status_ops.StatusContext:
    return status_ops.StatusContext(
        ansible_dir=tmp_path / "ansible",
        state_dir=tmp_path / "state",
        secrets_file=tmp_path / "ansible" / "secrets.yml",
        openclaw_source_mount="/Volumes/My Shared Files/openclaw-source",
        openclaw_payload_mount="/Volumes/My Shared Files/openclaw-payload",
        signal_payload_mount="/Volumes/My Shared Files/signal-cli-payload",
        signal_sync_label="com.clawbox.signal-cli-payload-sync",
        bootstrap_admin_user="admin",
        bootstrap_admin_password="admin",
        ansible_connect_timeout_seconds=8,
        ansible_command_timeout_seconds=30,
    )


def _capture(fn) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    return buf.getvalue()


def test_parse_mount_statuses_tolerates_blank_lines() -> None:
    paths = ["/a", "/b"]
    statuses = status_ops.parse_mount_statuses("\n\n/a=mounted\n", paths)
    assert statuses["/a"] == "mounted"
    assert statuses["/b"] == "unknown"


def test_format_mount_statuses_outputs_lines() -> None:
    rendered = status_ops.format_mount_statuses({"/a": "mounted", "/b": "dir"})
    assert "/a: mounted" in rendered
    assert "/b: dir" in rendered


def test_credential_candidates_warn_on_read_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _context(tmp_path)
    ctx.secrets_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.secrets_file.write_text("vm_password: ignored\n", encoding="utf-8")
    monkeypatch.setattr(status_ops, "read_vm_password", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("boom")))

    creds, warnings = status_ops._credential_candidates("clawbox-1", ctx)
    assert ("admin", "admin") in creds
    assert any("Could not read secrets file" in warning for warning in warnings)


def test_credential_candidates_dedupes_same_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _context(tmp_path)
    ctx.secrets_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.secrets_file.write_text("vm_password: admin\n", encoding="utf-8")
    monkeypatch.setattr(status_ops, "read_vm_password", lambda *_args, **_kwargs: "admin")

    creds, warnings = status_ops._credential_candidates("admin", ctx)
    assert warnings == []
    assert creds == [("admin", "admin")]


def test_status_mount_paths_for_standard_marker(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    marker = ProvisionMarker(
        vm_name="clawbox-1",
        profile="standard",
        playwright=False,
        tailscale=False,
        signal_cli=False,
        signal_payload=False,
        provisioned_at="2026-01-01T00:00:00Z",
    )
    paths, note = status_ops._status_mount_paths(marker, ctx)
    assert paths == []
    assert note is None


def test_probe_shared_mounts_not_applicable(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    probe, statuses, chosen = status_ops._probe_shared_mounts(
        "clawbox-1",
        [],
        [("user", "pw")],
        ctx,
    )
    assert probe == "not_applicable"
    assert statuses == {}
    assert chosen is None


def test_probe_shared_mounts_unavailable_when_no_parseable_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path)
    monkeypatch.setattr(
        status_ops,
        "_ansible_shell",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["ansible"], returncode=0, stdout="nonsense", stderr=""
        ),
    )
    probe, statuses, chosen = status_ops._probe_shared_mounts(
        "clawbox-1",
        [ctx.openclaw_source_mount],
        [("user", "pw")],
        ctx,
    )
    assert probe == "unavailable"
    assert statuses == {}
    assert chosen is None


def test_probe_signal_sync_daemon_no_credentials(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    probe, lines = status_ops._probe_signal_sync_daemon("clawbox-1", [], None, ctx)
    assert probe == "unavailable_no_credentials"
    assert lines == []


def test_probe_signal_sync_daemon_probe_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _context(tmp_path)
    monkeypatch.setattr(
        status_ops,
        "_ansible_shell",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["ansible"], returncode=1, stdout="", stderr="failed"
        ),
    )
    probe, lines = status_ops._probe_signal_sync_daemon(
        "clawbox-1",
        [("admin", "admin")],
        ("admin", "admin"),
        ctx,
    )
    assert probe == "unavailable_probe_failed"
    assert lines == []


def test_render_status_report_text_unavailable_branches(tmp_path: Path) -> None:
    marker_file = tmp_path / "state" / "clawbox-1.provisioned"
    marker_file.parent.mkdir(parents=True, exist_ok=True)
    marker_file.write_text("profile: developer\n", encoding="utf-8")

    marker = ProvisionMarker(
        vm_name="clawbox-1",
        profile="developer",
        playwright=False,
        tailscale=False,
        signal_cli=True,
        signal_payload=True,
        provisioned_at="2026-01-01T00:00:00Z",
    )
    report = status_ops.VMStatusReport(
        vm="clawbox-1",
        exists=True,
        running=True,
        provision_marker=status_ops.ProvisionMarkerReport(present=True, data={"profile": "developer"}),
        ip="192.168.64.10",
        shared_mounts=status_ops.SharedMountsReport(probe="unavailable"),
        signal_payload_sync=status_ops.SignalPayloadSyncReport(
            enabled=True,
            probe="unavailable_probe_failed",
            lines=[],
        ),
    )
    out = _capture(lambda: status_ops._render_status_report_text("clawbox-1", marker_file, marker, report))
    assert "shared mounts: unavailable" in out
    assert "signal payload sync daemon: unavailable (probe failed)" in out


def test_render_status_report_text_unavailable_no_credentials(tmp_path: Path) -> None:
    marker_file = tmp_path / "state" / "clawbox-1.provisioned"
    marker_file.parent.mkdir(parents=True, exist_ok=True)
    marker_file.write_text("profile: developer\n", encoding="utf-8")

    report = status_ops.VMStatusReport(
        vm="clawbox-1",
        exists=True,
        running=True,
        provision_marker=status_ops.ProvisionMarkerReport(present=True, data={"profile": "developer"}),
        ip="192.168.64.10",
        signal_payload_sync=status_ops.SignalPayloadSyncReport(
            enabled=True,
            probe="unavailable_no_credentials",
            lines=[],
        ),
    )
    out = _capture(lambda: status_ops._render_status_report_text("clawbox-1", marker_file, None, report))
    assert "signal payload sync daemon: unavailable (no credentials)" in out


def test_candidate_vm_names_include_tart_vms_and_marker_only(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    ctx.state_dir.mkdir(parents=True, exist_ok=True)
    (ctx.state_dir / "clawbox-3.provisioned").write_text("profile: standard\n", encoding="utf-8")
    tart = FakeTart(
        [
            {"Name": "clawbox-2", "Running": False},
            {"Name": "macos-base", "Running": False},
        ]
    )

    assert status_ops._candidate_vm_names(tart, ctx) == ["clawbox-2", "clawbox-3"]


def test_status_environment_json_no_vms(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    tart = FakeTart([])
    out = _capture(lambda: status_ops.status_environment(tart, as_json=True, context=ctx))
    payload = json.loads(out)
    assert payload["mode"] == "environment"
    assert payload["vm_count"] == 0
    assert payload["vms"] == []


def test_status_environment_text_includes_vm_sections(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    ctx.state_dir.mkdir(parents=True, exist_ok=True)
    marker = ProvisionMarker(
        vm_name="clawbox-2",
        profile="standard",
        playwright=True,
        tailscale=False,
        signal_cli=False,
        signal_payload=False,
        provisioned_at="2026-01-01T00:00:00Z",
    )
    marker.write(ctx.state_dir / "clawbox-2.provisioned")
    tart = FakeTart([{"Name": "clawbox-2", "Running": False}])
    out = _capture(lambda: status_ops.status_environment(tart, as_json=False, context=ctx))
    assert "Clawbox environment:" in out
    assert "VM: clawbox-2" in out
    assert "vms discovered: 1" in out
