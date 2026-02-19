from __future__ import annotations

import io
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from clawbox import status as status_ops
from clawbox.state import ProvisionMarker


MUTAGEN_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "mutagen"


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
        openclaw_source_mount="/Users/Shared/clawbox-sync/openclaw-source",
        openclaw_payload_mount="/Users/Shared/clawbox-sync/openclaw-payload",
        signal_payload_mount="/Users/Shared/clawbox-sync/signal-cli-payload",
        ansible_connect_timeout_seconds=8,
        ansible_command_timeout_seconds=30,
    )


def _capture(fn) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    return buf.getvalue()


def _mutagen_fixture(name: str) -> str:
    return (MUTAGEN_FIXTURES_DIR / name).read_text(encoding="utf-8")


def test_parse_mount_statuses_tolerates_blank_lines() -> None:
    paths = ["/a", "/b"]
    statuses = status_ops.parse_mount_statuses("\n\n/a=mounted\n", paths)
    assert statuses["/a"] == "mounted"
    assert statuses["/b"] == "unknown"


def test_format_mount_statuses_outputs_lines() -> None:
    rendered = status_ops.format_mount_statuses({"/a": "mounted", "/b": "dir"})
    assert "/a: mounted" in rendered
    assert "/b: dir" in rendered


def test_sync_probe_credentials_warn_on_read_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _context(tmp_path)
    ctx.secrets_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.secrets_file.write_text("vm_password: ignored\n", encoding="utf-8")
    monkeypatch.setattr(
        status_ops,
        "vm_user_credentials",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("boom")),
    )

    creds, warnings = status_ops._sync_probe_credentials("clawbox-91", ctx)
    assert creds is None
    assert any("Could not read secrets file" in warning for warning in warnings)


def test_sync_probe_credentials_resolves_vm_user_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _context(tmp_path)
    ctx.secrets_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.secrets_file.write_text("vm_password: admin\n", encoding="utf-8")
    monkeypatch.setattr(
        status_ops,
        "vm_user_credentials",
        lambda vm_name, **_kwargs: (vm_name, "vm-password"),
    )

    creds, warnings = status_ops._sync_probe_credentials("clawbox-91", ctx)
    assert warnings == []
    assert creds == ("clawbox-91", "vm-password")


def test_status_mount_paths_for_standard_marker(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    marker = ProvisionMarker(
        vm_name="clawbox-91",
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


def test_probe_sync_paths_not_applicable(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    probe, statuses = status_ops._probe_sync_paths(
        "clawbox-91",
        [],
        ansible_user="user",
        ansible_password="pw",
        context=ctx,
    )
    assert probe == "not_applicable"
    assert statuses == {}


def test_probe_sync_paths_unavailable_when_no_parseable_status(
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
    probe, statuses = status_ops._probe_sync_paths(
        "clawbox-91",
        [ctx.openclaw_source_mount],
        ansible_user="user",
        ansible_password="pw",
        context=ctx,
    )
    assert probe == "unavailable"
    assert statuses == {}


def test_probe_mutagen_sync_reports_inactive_when_sessions_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(status_ops, "mutagen_available", lambda: True)
    monkeypatch.setattr(
        status_ops,
        "vm_sessions_status",
        lambda _vm_name: _mutagen_fixture("sync-list-no-sessions.txt"),
    )
    probe, active, lines = status_ops._probe_mutagen_sync("clawbox-91")
    assert probe == "ok"
    assert active is False
    assert lines == ["no active sessions found"]


def test_probe_mutagen_sync_reports_unavailable_without_mutagen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(status_ops, "mutagen_available", lambda: False)
    probe, active, lines = status_ops._probe_mutagen_sync("clawbox-91")
    assert probe == "unavailable"
    assert active is None
    assert lines == ["mutagen CLI unavailable on host"]


def test_summarize_mutagen_status_extracts_name_and_status_lines() -> None:
    output = _mutagen_fixture("sync-list-active.txt")
    active, lines = status_ops._summarize_mutagen_status(output)
    assert active is True
    assert lines == [
        "Name: clawbox-clawbox-91-openclaw-source",
        "Status: Watching for changes",
        "Name: clawbox-clawbox-91-openclaw-payload",
        "Status: Watching for changes",
    ]


def test_summarize_mutagen_status_treats_unstructured_nonempty_output_as_active() -> None:
    output = _mutagen_fixture("sync-list-unstructured.txt")
    active, lines = status_ops._summarize_mutagen_status(output)
    assert active is True
    assert lines == ["unexpected mutagen output line", "another diagnostic line"]


def test_render_status_report_text_unavailable_branches(tmp_path: Path) -> None:
    marker_file = tmp_path / "state" / "clawbox-91.provisioned"
    marker_file.parent.mkdir(parents=True, exist_ok=True)
    marker_file.write_text("profile: developer\n", encoding="utf-8")

    marker = ProvisionMarker(
        vm_name="clawbox-91",
        profile="developer",
        playwright=False,
        tailscale=False,
        signal_cli=True,
        signal_payload=True,
        provisioned_at="2026-01-01T00:00:00Z",
    )
    report = status_ops.VMStatusReport(
        vm="clawbox-91",
        exists=True,
        running=True,
        provision_marker=status_ops.ProvisionMarkerReport(present=True, data={"profile": "developer"}),
        ip="192.168.64.10",
        sync_paths=status_ops.SyncPathsReport(probe="unavailable"),
        signal_payload_sync=status_ops.SignalPayloadSyncReport(
            enabled=True,
            probe="not_applicable",
            lines=[],
        ),
    )
    out = _capture(lambda: status_ops._render_status_report_text("clawbox-91", marker_file, marker, report))
    assert "sync paths: unavailable" in out
    assert "signal payload sync daemon:" not in out


def test_render_status_report_text_does_not_render_signal_daemon_section(tmp_path: Path) -> None:
    marker_file = tmp_path / "state" / "clawbox-91.provisioned"
    marker_file.parent.mkdir(parents=True, exist_ok=True)
    marker_file.write_text("profile: developer\n", encoding="utf-8")

    report = status_ops.VMStatusReport(
        vm="clawbox-91",
        exists=True,
        running=True,
        provision_marker=status_ops.ProvisionMarkerReport(present=True, data={"profile": "developer"}),
        ip="192.168.64.10",
        signal_payload_sync=status_ops.SignalPayloadSyncReport(
            enabled=True,
            probe="not_applicable",
            lines=[],
        ),
    )
    out = _capture(lambda: status_ops._render_status_report_text("clawbox-91", marker_file, None, report))
    assert "signal payload sync daemon:" not in out


def test_candidate_vm_names_include_tart_vms_and_marker_only(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    ctx.state_dir.mkdir(parents=True, exist_ok=True)
    (ctx.state_dir / "clawbox-93.provisioned").write_text("profile: standard\n", encoding="utf-8")
    tart = FakeTart(
        [
            {"Name": "clawbox-92", "Running": False},
            {"Name": "macos-base", "Running": False},
        ]
    )

    assert status_ops._candidate_vm_names(tart, ctx) == ["clawbox-92", "clawbox-93"]


def test_status_mount_paths_marker_missing_skips_remote_probe(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    paths, note = status_ops._status_mount_paths(None, ctx)
    assert paths == []
    assert note == "no marker found; skipping remote sync-path probe"


def test_status_vm_no_marker_does_not_call_remote_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _context(tmp_path)
    tart = FakeTart([{"Name": "clawbox-91", "Running": True, "IP": "192.168.64.10"}])
    called = {"probe": 0}
    monkeypatch.setattr(
        status_ops,
        "_probe_sync_paths",
        lambda *_args, **_kwargs: called.__setitem__("probe", called["probe"] + 1) or ("ok", {}),
    )

    out = _capture(lambda: status_ops.status_vm(91, tart, as_json=False, context=ctx))
    assert called["probe"] == 0
    assert "no marker found; skipping remote sync-path probe" in out


def test_status_vm_warns_when_mutagen_sessions_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path)
    ctx.state_dir.mkdir(parents=True, exist_ok=True)
    marker = ProvisionMarker(
        vm_name="clawbox-91",
        profile="developer",
        playwright=False,
        tailscale=False,
        signal_cli=True,
        signal_payload=True,
        provisioned_at="2026-01-01T00:00:00Z",
        sync_backend="mutagen",
    )
    marker.write(ctx.state_dir / "clawbox-91.provisioned")
    ctx.secrets_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.secrets_file.write_text("vm_password: admin\n", encoding="utf-8")

    tart = FakeTart([{"Name": "clawbox-91", "Running": True, "IP": "192.168.64.10"}])
    monkeypatch.setattr(
        status_ops,
        "_ansible_shell",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["ansible"],
            returncode=0,
            stdout=(
                f"{ctx.openclaw_source_mount}=dir\n"
                f"{ctx.openclaw_payload_mount}=dir\n"
                f"{ctx.signal_payload_mount}=dir\n"
            ),
            stderr="",
        ),
    )
    monkeypatch.setattr(status_ops, "mutagen_available", lambda: True)
    monkeypatch.setattr(
        status_ops,
        "vm_sessions_status",
        lambda _vm_name: _mutagen_fixture("sync-list-no-sessions.txt"),
    )

    out = _capture(lambda: status_ops.status_vm(91, tart, as_json=True, context=ctx))
    payload = json.loads(out)
    assert payload["mutagen_sync"]["enabled"] is True
    assert payload["mutagen_sync"]["probe"] == "ok"
    assert payload["mutagen_sync"]["active"] is False
    assert payload["mutagen_sync"]["lines"] == ["no active sessions found"]
    assert any("no active Mutagen sessions were found" in warning for warning in payload["warnings"])


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
        vm_name="clawbox-92",
        profile="standard",
        playwright=True,
        tailscale=False,
        signal_cli=False,
        signal_payload=False,
        provisioned_at="2026-01-01T00:00:00Z",
    )
    marker.write(ctx.state_dir / "clawbox-92.provisioned")
    tart = FakeTart([{"Name": "clawbox-92", "Running": False}])
    out = _capture(lambda: status_ops.status_environment(tart, as_json=False, context=ctx))
    assert "Clawbox environment:" in out
    assert "VM: clawbox-92" in out
    assert "vms discovered: 1" in out
