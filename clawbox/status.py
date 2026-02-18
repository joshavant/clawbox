from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Sequence

from clawbox.auth import vm_user_credentials
from clawbox.config import vm_base_name, vm_name_for
from clawbox.mutagen import MutagenError, mutagen_available, vm_sessions_status
from clawbox.remote_probe import RemoteShellContext, ansible_shell as ansible_shell_shared
from clawbox.secrets import missing_secrets_message
from clawbox.state import ProvisionMarker
from clawbox.tart import TartClient

MountProbeState = Literal["not_applicable", "ok", "unavailable"]
SignalProbeState = Literal["not_applicable"]
MutagenProbeState = Literal["not_applicable", "ok", "unavailable"]

_MOUNT_STATUS_RE = re.compile(r"['\"]?(?P<path>.+?)['\"]?=(?P<status>mounted|dir|missing|ok)")


@dataclass(frozen=True)
class StatusContext:
    ansible_dir: Path
    state_dir: Path
    secrets_file: Path
    openclaw_source_mount: str
    openclaw_payload_mount: str
    signal_payload_mount: str
    ansible_connect_timeout_seconds: int
    ansible_command_timeout_seconds: int


@dataclass
class ProvisionMarkerReport:
    present: bool
    data: dict[str, object] | None


@dataclass
class SyncPathsReport:
    note: str | None = None
    probe: MountProbeState = "not_applicable"
    paths: dict[str, str] = field(default_factory=dict)


@dataclass
class SignalPayloadSyncReport:
    enabled: bool
    probe: SignalProbeState = "not_applicable"
    lines: list[str] = field(default_factory=list)


@dataclass
class MutagenSyncReport:
    enabled: bool
    probe: MutagenProbeState = "not_applicable"
    active: bool | None = None
    lines: list[str] = field(default_factory=list)


@dataclass
class VMStatusReport:
    vm: str
    exists: bool
    running: bool
    provision_marker: ProvisionMarkerReport
    ip: str | None = None
    sync_paths: SyncPathsReport = field(default_factory=SyncPathsReport)
    signal_payload_sync: SignalPayloadSyncReport = field(
        default_factory=lambda: SignalPayloadSyncReport(enabled=False)
    )
    mutagen_sync: MutagenSyncReport = field(default_factory=lambda: MutagenSyncReport(enabled=False))
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        sync_paths_payload = {
            "note": self.sync_paths.note,
            "probe": self.sync_paths.probe,
            "paths": self.sync_paths.paths,
        }
        return {
            "vm": self.vm,
            "exists": self.exists,
            "running": self.running,
            "provision_marker": {
                "present": self.provision_marker.present,
                "data": self.provision_marker.data,
            },
            "ip": self.ip,
            "sync_paths": sync_paths_payload,
            "signal_payload_sync": {
                "enabled": self.signal_payload_sync.enabled,
                "probe": self.signal_payload_sync.probe,
                "lines": self.signal_payload_sync.lines,
            },
            "mutagen_sync": {
                "enabled": self.mutagen_sync.enabled,
                "probe": self.mutagen_sync.probe,
                "active": self.mutagen_sync.active,
                "lines": self.mutagen_sync.lines,
            },
            "warnings": self.warnings,
        }


def build_mount_status_command(mount_paths: Sequence[str]) -> str:
    clauses: list[str] = []
    for path in mount_paths:
        quoted_path = shlex.quote(path)
        mount_probe = shlex.quote(f" on {path} (")
        clauses.append(
            "if /sbin/mount | /usr/bin/grep -F -- "
            f"{mount_probe} >/dev/null 2>&1; then "
            f"printf '%s=%s\\n' {quoted_path} mounted; "
            f"elif [ -d {quoted_path} ]; then "
            f"printf '%s=%s\\n' {quoted_path} dir; "
            f"else printf '%s=%s\\n' {quoted_path} missing; fi"
        )
    return "; ".join(clauses)


def parse_mount_statuses(stdout: str, mount_paths: Sequence[str]) -> dict[str, str]:
    statuses = {path: "unknown" for path in mount_paths}
    mount_status_tokens = ("mounted", "dir", "missing", "ok")
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue

        for match in _MOUNT_STATUS_RE.finditer(line):
            path = match.group("path").strip().strip("'\"")
            status = match.group("status")
            if path in statuses:
                statuses[path] = status

        for path in mount_paths:
            if statuses[path] != "unknown":
                continue
            for status in mount_status_tokens:
                if f"{path}={status}" in line:
                    statuses[path] = status
                    break
    return statuses


def format_mount_statuses(statuses: dict[str, str]) -> str:
    return "\n".join(f"    - {path}: {status}" for path, status in statuses.items())


def _ansible_shell(
    vm_name: str,
    shell_cmd: str,
    *,
    ansible_user: str,
    ansible_password: str,
    become: bool,
    context: StatusContext,
) -> subprocess.CompletedProcess[str]:
    remote_context = RemoteShellContext(
        ansible_dir=context.ansible_dir,
        connect_timeout_seconds=context.ansible_connect_timeout_seconds,
        command_timeout_seconds=context.ansible_command_timeout_seconds,
        default_inventory_path="inventory/tart_inventory.py",
    )
    return ansible_shell_shared(
        vm_name,
        shell_cmd,
        ansible_user=ansible_user,
        ansible_password=ansible_password,
        become=become,
        context=remote_context,
    )


def _sync_probe_credentials(
    vm_name: str, context: StatusContext
) -> tuple[tuple[str, str] | None, list[str]]:
    try:
        return vm_user_credentials(
            vm_name,
            secrets_file=context.secrets_file,
        ), []
    except FileNotFoundError:
        return None, [missing_secrets_message(context.secrets_file)]
    except OSError as exc:
        return None, [f"Could not read secrets file '{context.secrets_file}': {exc}"]
    except ValueError as exc:
        return None, [str(exc)]


def _status_probe_allowed(marker: ProvisionMarker | None) -> bool:
    if marker is None:
        return False
    return marker.profile == "developer"


def _status_probe_auth(
    vm_name: str,
    marker: ProvisionMarker | None,
    context: StatusContext,
) -> tuple[tuple[str, str] | None, list[str]]:
    if not _status_probe_allowed(marker):
        return None, []
    return _sync_probe_credentials(vm_name, context)


def _status_report_base(
    vm_name: str,
    marker_file: Path,
    marker: ProvisionMarker | None,
    exists: bool,
    running: bool,
) -> VMStatusReport:
    marker_data = None
    if marker is not None:
        marker_data = {
            "profile": marker.profile,
            "playwright": marker.playwright,
            "tailscale": marker.tailscale,
            "signal_cli": marker.signal_cli,
            "signal_payload": marker.signal_payload,
            "sync_backend": marker.sync_backend,
        }
    return VMStatusReport(
        vm=vm_name,
        exists=exists,
        running=running,
        provision_marker=ProvisionMarkerReport(
            present=marker_file.exists(),
            data=marker_data,
        ),
        signal_payload_sync=SignalPayloadSyncReport(enabled=bool(marker and marker.signal_payload)),
        mutagen_sync=MutagenSyncReport(
            enabled=bool(marker and marker.profile == "developer" and marker.sync_backend == "mutagen")
        ),
    )


def _summarize_mutagen_status(status_output: str) -> tuple[bool, list[str]]:
    lines = [line.strip() for line in status_output.splitlines() if line.strip()]
    filtered = [line for line in lines if set(line) != {"-"}]
    if not filtered:
        return False, ["no active sessions found"]
    if any("No synchronization sessions found" in line for line in filtered):
        return False, ["no active sessions found"]
    session_summary = [line for line in filtered if line.startswith("Name: ") or line.startswith("Status: ")]
    if session_summary:
        return True, session_summary[:6]
    return True, filtered[:6]


def _probe_mutagen_sync(vm_name: str) -> tuple[MutagenProbeState, bool | None, list[str]]:
    if not mutagen_available():
        return "unavailable", None, ["mutagen CLI unavailable on host"]
    try:
        status_output = vm_sessions_status(vm_name)
    except MutagenError as exc:
        return "unavailable", None, [str(exc)]
    active, lines = _summarize_mutagen_status(status_output)
    return "ok", active, lines


def _status_mount_paths(
    marker: ProvisionMarker | None,
    context: StatusContext,
) -> tuple[list[str], str | None]:
    if marker and marker.profile == "developer":
        paths = [context.openclaw_source_mount, context.openclaw_payload_mount]
        if marker.signal_payload:
            paths.append(context.signal_payload_mount)
        return paths, None
    if marker:
        return [], None
    return ([], "no marker found; skipping remote sync-path probe")


def _probe_sync_paths(
    vm_name: str,
    mount_paths: Sequence[str],
    *,
    ansible_user: str,
    ansible_password: str,
    context: StatusContext,
) -> tuple[MountProbeState, dict[str, str]]:
    if not mount_paths:
        return "not_applicable", {}

    mount_cmd = build_mount_status_command(mount_paths)
    probe = _ansible_shell(
        vm_name,
        mount_cmd,
        ansible_user=ansible_user,
        ansible_password=ansible_password,
        become=False,
        context=context,
    )
    if probe.returncode != 0:
        return "unavailable", {}
    parsed_statuses = parse_mount_statuses(probe.stdout, mount_paths)
    if all(status == "unknown" for status in parsed_statuses.values()):
        return "unavailable", {}
    return "ok", parsed_statuses


def _render_status_report_text(
    vm_name: str,
    marker_file: Path,
    marker: ProvisionMarker | None,
    report: VMStatusReport,
) -> None:
    print(f"VM: {vm_name}")
    print(f"  exists: {'yes' if report.exists else 'no'}")
    print(f"  running: {'yes' if report.running else 'no'}")
    print(f"  provision marker: {'present' if marker_file.exists() else 'missing'}")
    if marker:
        print(
            "  marker profile/playwright/tailscale/signal_cli/signal_payload/sync_backend: "
            f"{marker.profile}/{str(marker.playwright).lower()}/{str(marker.tailscale).lower()}/"
            f"{str(marker.signal_cli).lower()}/{str(marker.signal_payload).lower()}/"
            f"{marker.sync_backend or '(missing)'}"
        )
    if report.warnings:
        print("  warnings:")
        for warning in report.warnings:
            print(f"    - {warning}")

    if not report.exists:
        return

    print(f"  ip: {report.ip if report.ip else '(unavailable)'}")
    if not report.running or not report.ip:
        return

    if report.sync_paths.note:
        print(f"  note: {report.sync_paths.note}")

    if report.sync_paths.probe == "unavailable":
        print("  sync paths: unavailable (remote probe failed)")
    elif report.sync_paths.probe == "ok":
        print("  sync paths:")
        for path, status in report.sync_paths.paths.items():
            print(f"    - {path}: {status}")

    if report.mutagen_sync.enabled:
        if report.mutagen_sync.probe == "unavailable":
            print("  mutagen sync: unavailable")
        elif report.mutagen_sync.probe == "ok":
            state = "active" if report.mutagen_sync.active else "inactive"
            print(f"  mutagen sync: {state}")
        for line in report.mutagen_sync.lines:
            print(f"    - {line}")

    return


def _build_vm_status_report(
    vm_name: str,
    tart: TartClient,
    *,
    context: StatusContext,
) -> tuple[Path, ProvisionMarker | None, VMStatusReport]:
    marker_file = context.state_dir / f"{vm_name}.provisioned"
    marker = ProvisionMarker.from_file(marker_file)
    exists = tart.vm_exists(vm_name)
    running = tart.vm_running(vm_name) if exists else False

    report = _status_report_base(vm_name, marker_file, marker, exists, running)
    if exists:
        report.ip = tart.ip(vm_name)

    if exists and running and report.ip:
        if report.mutagen_sync.enabled:
            mutagen_probe, mutagen_active, mutagen_lines = _probe_mutagen_sync(vm_name)
            report.mutagen_sync.probe = mutagen_probe
            report.mutagen_sync.active = mutagen_active
            report.mutagen_sync.lines = mutagen_lines
            if mutagen_probe == "ok" and mutagen_active is False:
                report.warnings.append(
                    "Mutagen sync backend is configured, but no active Mutagen sessions were found."
                )
            if mutagen_probe == "unavailable":
                report.warnings.append("Mutagen sync status is unavailable.")

        mount_paths, mount_note = _status_mount_paths(marker, context)
        if mount_note:
            report.sync_paths.note = mount_note

        creds, warnings = _status_probe_auth(vm_name, marker, context)
        report.warnings.extend(warnings)
        if not mount_paths:
            return marker_file, marker, report
        if creds is None:
            report.sync_paths.probe = "unavailable"
            return marker_file, marker, report
        mount_probe, mount_statuses = _probe_sync_paths(
            vm_name,
            mount_paths,
            ansible_user=creds[0],
            ansible_password=creds[1],
            context=context,
        )
        report.sync_paths.probe = mount_probe
        report.sync_paths.paths = mount_statuses

    return marker_file, marker, report


def _parse_vm_suffix_number(vm_name: str, base_name: str) -> int | None:
    prefix = f"{base_name}-"
    if not vm_name.startswith(prefix):
        return None
    suffix = vm_name[len(prefix) :]
    if not suffix.isdigit():
        return None
    value = int(suffix)
    if value < 1:
        return None
    return value


def _candidate_vm_names(tart: TartClient, context: StatusContext) -> list[str]:
    base_name = vm_base_name()
    names: set[str] = set()

    for vm in tart.list_vms_json():
        name = vm.get("Name")
        if not isinstance(name, str):
            continue
        if _parse_vm_suffix_number(name, base_name) is not None:
            names.add(name)

    if context.state_dir.exists():
        for marker_file in context.state_dir.glob(f"{base_name}-*.provisioned"):
            vm_name = marker_file.name.removesuffix(".provisioned")
            if _parse_vm_suffix_number(vm_name, base_name) is not None:
                names.add(vm_name)

    return sorted(
        names,
        key=lambda name: (_parse_vm_suffix_number(name, base_name) or 10**9, name),
    )


def status_vm(vm_number: int, tart: TartClient, *, as_json: bool, context: StatusContext) -> None:
    vm_name = vm_name_for(vm_number)
    marker_file, marker, report = _build_vm_status_report(vm_name, tart, context=context)

    if as_json:
        print(json.dumps(report.as_dict(), indent=2))
        return

    _render_status_report_text(vm_name, marker_file, marker, report)


def status_environment(tart: TartClient, *, as_json: bool, context: StatusContext) -> None:
    vm_names = _candidate_vm_names(tart, context)
    reports: list[VMStatusReport] = []
    marker_files: dict[str, Path] = {}
    markers: dict[str, ProvisionMarker | None] = {}

    for vm_name in vm_names:
        marker_file, marker, report = _build_vm_status_report(vm_name, tart, context=context)
        marker_files[vm_name] = marker_file
        markers[vm_name] = marker
        reports.append(report)

    if as_json:
        payload = {
            "mode": "environment",
            "vm_count": len(reports),
            "running_count": sum(1 for report in reports if report.running),
            "vms": [report.as_dict() for report in reports],
        }
        print(json.dumps(payload, indent=2))
        return

    print("Clawbox environment:")
    print(f"  vms discovered: {len(reports)}")

    if not reports:
        print("  no Clawbox VMs found.")
        print("  run `clawbox up` to create and provision one.")
        return

    for report in reports:
        print("")
        _render_status_report_text(
            report.vm,
            marker_files[report.vm],
            markers[report.vm],
            report,
        )
