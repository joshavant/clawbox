from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Sequence

from clawbox.ansible_exec import run_ansible_shell
from clawbox.config import vm_base_name, vm_name_for
from clawbox.secrets import read_vm_password
from clawbox.state import ProvisionMarker
from clawbox.tart import TartClient

MountProbeState = Literal["not_applicable", "ok", "unavailable"]
SignalProbeState = Literal[
    "not_applicable", "ok", "unavailable_no_credentials", "unavailable_probe_failed"
]

_MOUNT_STATUS_RE = re.compile(r"['\"]?(?P<path>.+?)['\"]?=(?P<status>mounted|dir|missing|ok)")


@dataclass(frozen=True)
class StatusContext:
    ansible_dir: Path
    state_dir: Path
    secrets_file: Path
    openclaw_source_mount: str
    openclaw_payload_mount: str
    signal_payload_mount: str
    signal_sync_label: str
    bootstrap_admin_user: str
    bootstrap_admin_password: str
    ansible_connect_timeout_seconds: int
    ansible_command_timeout_seconds: int


@dataclass
class ProvisionMarkerReport:
    present: bool
    data: dict[str, object] | None


@dataclass
class SharedMountsReport:
    note: str | None = None
    probe: MountProbeState = "not_applicable"
    paths: dict[str, str] = field(default_factory=dict)


@dataclass
class SignalPayloadSyncReport:
    enabled: bool
    probe: SignalProbeState = "not_applicable"
    lines: list[str] = field(default_factory=list)


@dataclass
class VMStatusReport:
    vm: str
    exists: bool
    running: bool
    provision_marker: ProvisionMarkerReport
    ip: str | None = None
    shared_mounts: SharedMountsReport = field(default_factory=SharedMountsReport)
    signal_payload_sync: SignalPayloadSyncReport = field(
        default_factory=lambda: SignalPayloadSyncReport(enabled=False)
    )
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "vm": self.vm,
            "exists": self.exists,
            "running": self.running,
            "provision_marker": {
                "present": self.provision_marker.present,
                "data": self.provision_marker.data,
            },
            "ip": self.ip,
            "shared_mounts": {
                "note": self.shared_mounts.note,
                "probe": self.shared_mounts.probe,
                "paths": self.shared_mounts.paths,
            },
            "signal_payload_sync": {
                "enabled": self.signal_payload_sync.enabled,
                "probe": self.signal_payload_sync.probe,
                "lines": self.signal_payload_sync.lines,
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
    return run_ansible_shell(
        ansible_dir=context.ansible_dir,
        inventory_path="inventory/tart_inventory.py",
        vm_name=vm_name,
        shell_cmd=shell_cmd,
        ansible_user=ansible_user,
        ansible_password=ansible_password,
        connect_timeout_seconds=context.ansible_connect_timeout_seconds,
        command_timeout_seconds=context.ansible_command_timeout_seconds,
        become=become,
    )


def _credential_candidates(
    vm_name: str, context: StatusContext
) -> tuple[list[tuple[str, str]], list[str]]:
    candidates: list[tuple[str, str]] = []
    warnings: list[str] = []
    if context.secrets_file.exists():
        try:
            candidates.append((vm_name, read_vm_password(context.secrets_file)))
        except OSError as exc:
            warnings.append(f"Could not read secrets file '{context.secrets_file}': {exc}")
        except ValueError as exc:
            warnings.append(str(exc))
    candidates.append((context.bootstrap_admin_user, context.bootstrap_admin_password))

    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for cred in candidates:
        if cred in seen:
            continue
        seen.add(cred)
        deduped.append(cred)
    return deduped, warnings


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
    )


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
    return (
        [context.openclaw_source_mount, context.openclaw_payload_mount, context.signal_payload_mount],
        "no marker found; probing all known shared mount paths",
    )


def _probe_shared_mounts(
    vm_name: str,
    mount_paths: Sequence[str],
    creds: Sequence[tuple[str, str]],
    context: StatusContext,
) -> tuple[MountProbeState, dict[str, str], tuple[str, str] | None]:
    if not mount_paths:
        return "not_applicable", {}, None

    mount_cmd = build_mount_status_command(mount_paths)
    for user, password in creds:
        probe = _ansible_shell(
            vm_name,
            mount_cmd,
            ansible_user=user,
            ansible_password=password,
            become=False,
            context=context,
        )
        if probe.returncode != 0:
            continue
        parsed_statuses = parse_mount_statuses(probe.stdout, mount_paths)
        if all(status == "unknown" for status in parsed_statuses.values()):
            continue
        return "ok", parsed_statuses, (user, password)

    return "unavailable", {}, None


def _probe_signal_sync_daemon(
    vm_name: str,
    creds: Sequence[tuple[str, str]],
    chosen: tuple[str, str] | None,
    context: StatusContext,
) -> tuple[SignalProbeState, list[str]]:
    credential = chosen
    if credential is None and creds:
        credential = creds[0]
    if credential is None:
        return "unavailable_no_credentials", []

    daemon_cmd = (
        f"(launchctl print system/{context.signal_sync_label} 2>&1 | "
        "/usr/bin/egrep 'state =|pid =|Could not find service' || true); "
        f"/usr/bin/tail -n 5 /tmp/{context.signal_sync_label}.log 2>/dev/null || true"
    )
    daemon_probe = _ansible_shell(
        vm_name,
        daemon_cmd,
        ansible_user=credential[0],
        ansible_password=credential[1],
        become=True,
        context=context,
    )
    if daemon_probe.returncode != 0:
        return "unavailable_probe_failed", []
    return (
        "ok",
        [line.rstrip() for line in daemon_probe.stdout.splitlines() if line.strip()],
    )


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
            "  marker profile/playwright/tailscale/signal_cli/signal_payload: "
            f"{marker.profile}/{str(marker.playwright).lower()}/{str(marker.tailscale).lower()}/"
            f"{str(marker.signal_cli).lower()}/{str(marker.signal_payload).lower()}"
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

    if report.shared_mounts.note:
        print(f"  note: {report.shared_mounts.note}")

    if report.shared_mounts.probe == "unavailable":
        print("  shared mounts: unavailable (remote probe failed)")
    elif report.shared_mounts.probe == "ok":
        print("  shared mounts:")
        for path, status in report.shared_mounts.paths.items():
            print(f"    - {path}: {status}")

    if not report.signal_payload_sync.enabled:
        return
    if report.signal_payload_sync.probe == "unavailable_no_credentials":
        print("  signal payload sync daemon: unavailable (no credentials)")
        return
    if report.signal_payload_sync.probe == "unavailable_probe_failed":
        print("  signal payload sync daemon: unavailable (probe failed)")
        return
    if report.signal_payload_sync.probe == "ok":
        print("  signal payload sync daemon:")
        for line in report.signal_payload_sync.lines:
            print(f"    {line}")


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
        mount_paths, mount_note = _status_mount_paths(marker, context)
        if mount_note:
            report.shared_mounts.note = mount_note

        creds, warnings = _credential_candidates(vm_name, context)
        report.warnings.extend(warnings)
        mount_probe, mount_statuses, chosen = _probe_shared_mounts(vm_name, mount_paths, creds, context)
        report.shared_mounts.probe = mount_probe
        report.shared_mounts.paths = mount_statuses

        if marker and marker.signal_payload:
            daemon_probe, daemon_lines = _probe_signal_sync_daemon(vm_name, creds, chosen, context)
            report.signal_payload_sync.probe = daemon_probe
            report.signal_payload_sync.lines = daemon_lines

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
