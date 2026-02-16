from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from clawbox.io_utils import atomic_write_text, read_text_or_empty
from clawbox.tart import TartClient, TartError


_MUTAGEN_SSH_CONFIG_INCLUDE = "Include ~/.ssh/clawbox_mutagen_config"
_MUTAGEN_SSH_CONFIG_PATH = Path.home() / ".ssh" / "clawbox_mutagen_config"
_MUTAGEN_ACTIVE_VMS_PATH = Path("mutagen") / "active_vms.json"
_SOURCE_KIND = "openclaw-source"
_PAYLOAD_KIND = "openclaw-payload"


class MutagenError(RuntimeError):
    """Raised for mutagen lifecycle failures."""


@dataclass(frozen=True)
class SessionSpec:
    kind: str
    host_path: Path
    guest_path: str
    ignore_vcs: bool = False
    ignored_paths: tuple[str, ...] = ()
    ready_required: bool = True


def mutagen_available() -> bool:
    return shutil.which("mutagen") is not None


def _run_mutagen(
    args: list[str],
    *,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["mutagen", *args],
            check=False,
            text=True,
            capture_output=capture_output,
        )
    except FileNotFoundError as exc:
        raise MutagenError("Error: Command not found: mutagen") from exc
    except OSError as exc:
        cmd = " ".join(["mutagen", *args])
        raise MutagenError(f"Error: Could not run command '{cmd}': {exc}") from exc

    if check and proc.returncode != 0:
        cmd = " ".join(["mutagen", *args])
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        details = stderr or stdout
        if details:
            raise MutagenError(f"Error: Command failed (exit {proc.returncode}): {cmd}\n{details}")
        raise MutagenError(f"Error: Command failed (exit {proc.returncode}): {cmd}")
    return proc


def _read_text(path: Path) -> str:
    return read_text_or_empty(path)


def _sanitize_vm_name(vm_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "-", vm_name)


def mutagen_ssh_alias(vm_name: str) -> str:
    return f"clawbox-mutagen-{_sanitize_vm_name(vm_name)}"


def _session_name(vm_name: str, kind: str) -> str:
    return f"clawbox-{_sanitize_vm_name(vm_name)}-{kind}"


def _vm_label(vm_name: str) -> str:
    return f"clawbox.vm={vm_name}"


def _upsert_named_block(path: Path, begin_marker: str, end_marker: str, block: str) -> None:
    existing = _read_text(path)
    lines = existing.splitlines()
    kept: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == begin_marker:
            i += 1
            while i < len(lines) and lines[i].strip() != end_marker:
                i += 1
            if i < len(lines):
                i += 1
            continue
        kept.append(lines[i])
        i += 1

    while kept and kept[-1] == "":
        kept.pop()
    rendered = ("\n".join(kept) + "\n\n" if kept else "") + block.rstrip() + "\n"
    atomic_write_text(path, rendered)


def _remove_named_block(path: Path, begin_marker: str, end_marker: str) -> None:
    existing = _read_text(path)
    if not existing:
        return
    lines = existing.splitlines()
    kept: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == begin_marker:
            i += 1
            while i < len(lines) and lines[i].strip() != end_marker:
                i += 1
            if i < len(lines):
                i += 1
            continue
        kept.append(lines[i])
        i += 1
    while kept and kept[-1] == "":
        kept.pop()
    rendered = "\n".join(kept)
    if rendered:
        rendered += "\n"
    atomic_write_text(path, rendered)


def _ensure_main_ssh_config_include() -> None:
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    main_config = ssh_dir / "config"
    existing = _read_text(main_config)
    if _MUTAGEN_SSH_CONFIG_INCLUDE in existing:
        return
    if existing and not existing.endswith("\n"):
        existing += "\n"
    updated = existing + _MUTAGEN_SSH_CONFIG_INCLUDE + "\n"
    atomic_write_text(main_config, updated)


def ensure_mutagen_ssh_alias(vm_name: str, vm_ip: str, vm_user: str, identity_file: Path) -> str:
    _ensure_main_ssh_config_include()
    alias = mutagen_ssh_alias(vm_name)
    begin = f"# CLAWBOX MUTAGEN BEGIN {vm_name}"
    end = f"# CLAWBOX MUTAGEN END {vm_name}"
    block = "\n".join(
        [
            begin,
            f"Host {alias}",
            f"  HostName {vm_ip}",
            f"  User {vm_user}",
            "  Port 22",
            f"  IdentityFile {identity_file}",
            "  IdentitiesOnly yes",
            "  StrictHostKeyChecking no",
            "  UserKnownHostsFile /dev/null",
            "  LogLevel ERROR",
            end,
        ]
    )
    _upsert_named_block(_MUTAGEN_SSH_CONFIG_PATH, begin, end, block)
    return alias


def remove_mutagen_ssh_alias(vm_name: str) -> None:
    begin = f"# CLAWBOX MUTAGEN BEGIN {vm_name}"
    end = f"# CLAWBOX MUTAGEN END {vm_name}"
    _remove_named_block(_MUTAGEN_SSH_CONFIG_PATH, begin, end)


def terminate_vm_sessions(vm_name: str, *, flush: bool) -> None:
    if not mutagen_available():
        return
    selector = _vm_label(vm_name)
    if flush:
        _run_mutagen(["sync", "flush", "--label-selector", selector], check=False)
    _run_mutagen(["sync", "terminate", "--label-selector", selector], check=False)


def ensure_vm_sessions(vm_name: str, alias: str, specs: list[SessionSpec]) -> None:
    if not mutagen_available():
        raise MutagenError("Error: Command not found: mutagen")

    session_names: list[str] = []
    for spec in specs:
        session_name = _session_name(vm_name, spec.kind)
        session_names.append(session_name)
        _run_mutagen(["sync", "terminate", session_name], check=False)
        args = [
            "sync",
            "create",
            "--name",
            session_name,
            "--mode",
            "two-way-resolved",
            "--label",
            _vm_label(vm_name),
            "--label",
            "clawbox.managed=true",
            "--label",
            f"clawbox.kind={spec.kind}",
        ]
        if spec.ignore_vcs:
            args += ["--ignore-vcs"]
        for ignored_path in spec.ignored_paths:
            args += ["--ignore", ignored_path]
        args += [str(spec.host_path), f"{alias}:{spec.guest_path}"]
        _run_mutagen(args, check=True)
    if session_names:
        _run_mutagen(["sync", "flush", "--label-selector", _vm_label(vm_name)], check=True)


def vm_sessions_exist(vm_name: str) -> bool:
    if not mutagen_available():
        return False
    proc = _run_mutagen(
        [
            "sync",
            "list",
            "--label-selector",
            _vm_label(vm_name),
            "--template",
            '{{range .}}{{.Identifier}}{{"\\n"}}{{end}}',
        ],
        check=False,
    )
    return bool((proc.stdout or "").strip())


def vm_sessions_status(vm_name: str) -> str:
    if not mutagen_available():
        return "mutagen not available"
    proc = _run_mutagen(
        ["sync", "list", "-l", "--label-selector", _vm_label(vm_name)],
        check=False,
    )
    output = (proc.stdout or "").strip()
    if output:
        return output
    return (proc.stderr or "").strip()


def _active_vms_registry_path(state_dir: Path) -> Path:
    return state_dir / _MUTAGEN_ACTIVE_VMS_PATH


def _read_active_vms(path: Path) -> list[str]:
    raw = _read_text(path)
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    vms = payload.get("vms")
    if not isinstance(vms, list):
        return []
    clean: list[str] = []
    for vm in vms:
        if isinstance(vm, str) and vm:
            clean.append(vm)
    return sorted(set(clean))


def _write_active_vms(path: Path, vms: list[str]) -> None:
    atomic_write_text(path, json.dumps({"vms": sorted(set(vms))}, sort_keys=True) + "\n")


def mark_vm_active(state_dir: Path, vm_name: str) -> None:
    registry = _active_vms_registry_path(state_dir)
    vms = _read_active_vms(registry)
    if vm_name not in vms:
        vms.append(vm_name)
    _write_active_vms(registry, vms)


def clear_vm_active(state_dir: Path, vm_name: str) -> None:
    registry = _active_vms_registry_path(state_dir)
    vms = [name for name in _read_active_vms(registry) if name != vm_name]
    _write_active_vms(registry, vms)


def active_vms(state_dir: Path) -> list[str]:
    return _read_active_vms(_active_vms_registry_path(state_dir))


def teardown_vm_sync(state_dir: Path, vm_name: str, *, flush: bool) -> None:
    terminate_vm_sessions(vm_name, flush=flush)
    clear_vm_active(state_dir, vm_name)
    remove_mutagen_ssh_alias(vm_name)


def reconcile_vm_sync(tart: TartClient, state_dir: Path) -> None:
    for vm_name in active_vms(state_dir):
        try:
            running = tart.vm_running(vm_name)
        except TartError:
            continue
        if not running:
            teardown_vm_sync(state_dir, vm_name, flush=False)
