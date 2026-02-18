from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Sequence

from clawbox.auth import vm_user_credentials
from clawbox.config import group_var_scalar, vm_name_for
from clawbox.errors import UserFacingError, main_guard
from clawbox.image import image_build, image_init
from clawbox.io_utils import tail_lines
from clawbox.locks import (
    LockSpec,
    OPENCLAW_PAYLOAD_LOCK,
    OPENCLAW_SOURCE_LOCK,
    SIGNAL_PAYLOAD_LOCK,
    LockError,
    acquire_path_lock,
    cleanup_locks_for_vm,
    locked_path_for_vm,
)
from clawbox.mutagen import (
    MutagenError,
    SessionSpec,
    ensure_mutagen_ssh_alias,
    ensure_vm_sessions,
    mark_vm_active as mark_mutagen_vm_active,
    mutagen_available,
    reconcile_vm_sync,
    teardown_vm_sync,
    vm_sessions_status,
)
from clawbox.secrets import (
    ensure_vm_password_file,
    missing_secrets_message,
)
from clawbox.sync_events import emit_sync_event
from clawbox.paths import default_secrets_file, default_state_dir, resolve_data_root
from clawbox.remote_probe import (
    RemoteShellContext,
    ansible_shell as ansible_shell_shared,
    run_remote_path_probe as run_remote_path_probe_shared,
    wait_for_remote_probe as wait_for_remote_probe_shared,
)
from clawbox.services import (
    OPTIONAL_SERVICES,
    enabled_optional_service_keys,
    unsupported_optional_services,
)
from clawbox.status import (
    StatusContext,
    build_mount_status_command,
    format_mount_statuses,
    parse_mount_statuses,
    status_environment as status_environment_impl,
    status_vm as status_vm_impl,
)
from clawbox.state import ProvisionMarker, current_utc_timestamp
from clawbox.tart import TartClient, TartError, wait_for_vm_running
from clawbox.watcher import (
    WatcherError,
    reconcile_vm_watchers,
    run_vm_watcher_loop,
    start_vm_watcher,
    stop_vm_watcher,
)


PROJECT_DIR = resolve_data_root()
ANSIBLE_DIR = PROJECT_DIR / "ansible"
SECRETS_FILE = default_secrets_file(PROJECT_DIR)
STATE_DIR = default_state_dir(PROJECT_DIR)
BASE_IMAGE = "macos-base"
DEFAULT_OPENCLAW_SOURCE_MOUNT = "/Users/Shared/clawbox-sync/openclaw-source"
DEFAULT_OPENCLAW_PAYLOAD_MOUNT = "/Users/Shared/clawbox-sync/openclaw-payload"
DEFAULT_SIGNAL_PAYLOAD_MOUNT = "/Users/Shared/clawbox-sync/signal-cli-payload"
DEFAULT_SIGNAL_PAYLOAD_MARKER_FILENAME = ".clawbox-signal-payload-host-marker"
DEFAULT_BOOTSTRAP_ADMIN_USER = "admin"
DEFAULT_BOOTSTRAP_ADMIN_PASSWORD = "admin"
OPENCLAW_SOURCE_MOUNT = group_var_scalar("openclaw_source_mount", DEFAULT_OPENCLAW_SOURCE_MOUNT)
OPENCLAW_PAYLOAD_MOUNT = group_var_scalar("openclaw_payload_mount", DEFAULT_OPENCLAW_PAYLOAD_MOUNT)
SIGNAL_PAYLOAD_MOUNT = group_var_scalar("signal_cli_payload_mount", DEFAULT_SIGNAL_PAYLOAD_MOUNT)
SIGNAL_PAYLOAD_MARKER_FILENAME = group_var_scalar(
    "signal_cli_payload_marker_filename", DEFAULT_SIGNAL_PAYLOAD_MARKER_FILENAME
)
BOOTSTRAP_ADMIN_USER = group_var_scalar("bootstrap_admin_user", DEFAULT_BOOTSTRAP_ADMIN_USER)
BOOTSTRAP_ADMIN_PASSWORD = group_var_scalar(
    "bootstrap_admin_password", DEFAULT_BOOTSTRAP_ADMIN_PASSWORD
)
REQUIRED_DEVELOPER_SYNC_BACKEND = "mutagen"
MutagenAuthMode = Literal["vm_user", "bootstrap_admin"]


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


ANSIBLE_CONNECT_TIMEOUT_SECONDS = _env_int("CLAWBOX_ANSIBLE_CONNECT_TIMEOUT_SECONDS", 8)
ANSIBLE_COMMAND_TIMEOUT_SECONDS = _env_int("CLAWBOX_ANSIBLE_COMMAND_TIMEOUT_SECONDS", 30)
MUTAGEN_READY_TIMEOUT_SECONDS = _env_int("CLAWBOX_MUTAGEN_READY_TIMEOUT_SECONDS", 60)

def _status_context() -> StatusContext:
    return StatusContext(
        ansible_dir=ANSIBLE_DIR,
        state_dir=STATE_DIR,
        secrets_file=SECRETS_FILE,
        openclaw_source_mount=OPENCLAW_SOURCE_MOUNT,
        openclaw_payload_mount=OPENCLAW_PAYLOAD_MOUNT,
        signal_payload_mount=SIGNAL_PAYLOAD_MOUNT,
        ansible_connect_timeout_seconds=ANSIBLE_CONNECT_TIMEOUT_SECONDS,
        ansible_command_timeout_seconds=ANSIBLE_COMMAND_TIMEOUT_SECONDS,
    )


def ensure_secrets_file(create_if_missing: bool) -> None:
    try:
        created = ensure_vm_password_file(SECRETS_FILE, create_if_missing=create_if_missing)
    except FileNotFoundError as exc:
        raise UserFacingError(missing_secrets_message(SECRETS_FILE)) from exc
    except OSError as exc:
        raise UserFacingError(f"Error: Could not write secrets file '{SECRETS_FILE}': {exc}") from exc

    if created:
        print(f"Created secrets file: {SECRETS_FILE}")


def _validate_profile(profile: str) -> None:
    if profile not in {"standard", "developer"}:
        raise UserFacingError("Error: --profile must be 'standard' or 'developer'")


def _validate_profile_mount_args(
    profile: str, openclaw_source: str, openclaw_payload: str, signal_payload: str
) -> None:
    if profile == "developer":
        if not openclaw_source or not openclaw_payload:
            raise UserFacingError(
                "Error: Developer profile requires --openclaw-source and --openclaw-payload."
            )
        return

    if openclaw_source or openclaw_payload:
        raise UserFacingError(
            "Error: --openclaw-source/--openclaw-payload are only valid in developer mode."
        )
    if signal_payload:
        raise UserFacingError("Error: --signal-cli-payload is only valid in developer mode.")


def _validate_feature_flags(
    profile: str,
    *,
    enable_playwright: bool,
    enable_tailscale: bool,
    enable_signal_cli: bool,
    enable_signal_payload: bool,
    signal_payload: str = "",
) -> None:
    enabled_services = enabled_optional_service_keys(
        enable_playwright=enable_playwright,
        enable_tailscale=enable_tailscale,
        enable_signal_cli=enable_signal_cli,
    )
    unsupported = unsupported_optional_services(profile, enabled_services)
    if unsupported:
        names = ", ".join(spec.display_name for spec in unsupported)
        profiles = ", ".join(sorted(set().union(*(spec.allowed_profiles for spec in unsupported))))
        raise UserFacingError(
            f"Error: {names} provisioning is not supported for profile '{profile}'.\n"
            f"Supported profiles: {profiles}"
        )

    if enable_signal_payload and profile != "developer":
        raise UserFacingError(
            "Error: signal-cli payload mode is only valid in developer mode.\n"
            "Standard mode supports signal-cli provisioning only (no custom payload mounts)."
        )

    if enable_signal_payload and not enable_signal_cli:
        payload_flag = "--signal-cli-payload" if signal_payload else "--enable-signal-payload"
        raise UserFacingError(
            f"Error: {payload_flag} requires --add-signal-cli-provisioning.\n"
            "Enable signal-cli provisioning explicitly when using payload mode."
        )


def _validate_dirs(paths: Sequence[str]) -> None:
    for path in paths:
        if path and not Path(path).is_dir():
            raise UserFacingError(f"Error: Expected directory does not exist: {path}")


def _render_vm_path(template: str, vm_name: str, default_template: str) -> str:
    value = template or default_template
    value = value.replace("{{ vm_name }}", vm_name)
    value = value.replace("{{vm_name}}", vm_name)
    return value


@dataclass(frozen=True)
class SyncPathBinding:
    kind: str
    lock: LockSpec
    guest_path_template: str
    default_guest_path_template: str
    ignore_vcs: bool = False
    ignored_paths: tuple[str, ...] = ()
    ready_required: bool = True
    required: bool = True


SYNC_PATH_BINDINGS = (
    SyncPathBinding(
        kind="openclaw-source",
        lock=OPENCLAW_SOURCE_LOCK,
        guest_path_template=OPENCLAW_SOURCE_MOUNT,
        default_guest_path_template=DEFAULT_OPENCLAW_SOURCE_MOUNT,
        ignore_vcs=True,
        ignored_paths=("node_modules", "dist"),
        required=True,
    ),
    SyncPathBinding(
        kind="openclaw-payload",
        lock=OPENCLAW_PAYLOAD_LOCK,
        guest_path_template=OPENCLAW_PAYLOAD_MOUNT,
        default_guest_path_template=DEFAULT_OPENCLAW_PAYLOAD_MOUNT,
        required=True,
    ),
    SyncPathBinding(
        kind="signal-payload",
        lock=SIGNAL_PAYLOAD_LOCK,
        guest_path_template=SIGNAL_PAYLOAD_MOUNT,
        default_guest_path_template=DEFAULT_SIGNAL_PAYLOAD_MOUNT,
        required=False,
    ),
)


def _build_sync_specs(vm_name: str, host_paths_by_lock: dict[LockSpec, str]) -> list[SessionSpec]:
    specs: list[SessionSpec] = []
    for binding in SYNC_PATH_BINDINGS:
        host_path = host_paths_by_lock.get(binding.lock, "")
        if not host_path:
            if binding.required:
                raise UserFacingError(
                    "Error: Missing required developer sync path.\n"
                    f"  kind: {binding.kind}\n"
                    f"  flag: {binding.lock.arg_hint}"
                )
            continue
        guest_path = _render_vm_path(
            binding.guest_path_template, vm_name, binding.default_guest_path_template
        )
        specs.append(
            SessionSpec(
                kind=binding.kind,
                host_path=Path(host_path).expanduser().resolve(),
                guest_path=guest_path,
                ignore_vcs=binding.ignore_vcs,
                ignored_paths=binding.ignored_paths,
                ready_required=binding.ready_required,
            )
        )
    return specs


def _host_paths_from_args(
    openclaw_source: str, openclaw_payload: str, signal_payload: str
) -> dict[LockSpec, str]:
    return {
        OPENCLAW_SOURCE_LOCK: openclaw_source,
        OPENCLAW_PAYLOAD_LOCK: openclaw_payload,
        SIGNAL_PAYLOAD_LOCK: signal_payload,
    }


def _host_paths_from_locks(vm_name: str) -> dict[LockSpec, str]:
    return {
        OPENCLAW_SOURCE_LOCK: locked_path_for_vm(OPENCLAW_SOURCE_LOCK, vm_name),
        OPENCLAW_PAYLOAD_LOCK: locked_path_for_vm(OPENCLAW_PAYLOAD_LOCK, vm_name),
        SIGNAL_PAYLOAD_LOCK: locked_path_for_vm(SIGNAL_PAYLOAD_LOCK, vm_name),
    }


def _with_virtualization_limit_hint(message: str) -> str:
    lowered = message.lower()
    indicators = (
        "vzerrordomain",
        "virtualization",
        "virtual machine limit",
        "system limit",
        "exceeds the system limit",
        "maximum number of virtual machines",
        "resource busy",
    )
    if not any(token in lowered for token in indicators):
        return message
    return (
        f"{message}\n"
        "Hint: macOS Virtualization.framework may be refusing another VM on this host.\n"
        "Stop other VMs and retry (for example: clawbox down 1, clawbox down 2)."
    )


def _ensure_signal_payload_host_marker(signal_payload_host: str, vm_name: str) -> None:
    marker_path = Path(signal_payload_host) / SIGNAL_PAYLOAD_MARKER_FILENAME
    marker_content = (
        "This marker is used by Clawbox to verify signal-cli payload sync destination readiness.\n"
        f"vm: {vm_name}\n"
    )
    try:
        marker_path.write_text(marker_content, encoding="utf-8")
    except OSError as exc:
        raise UserFacingError(
            f"Error: Could not write signal payload marker file: {marker_path}\n{exc}"
        ) from exc


def _acquire_locks(
    tart: TartClient,
    vm_name: str,
    openclaw_source: str,
    openclaw_payload: str,
    signal_payload: str,
) -> None:
    try:
        if openclaw_source:
            acquire_path_lock(OPENCLAW_SOURCE_LOCK, vm_name, openclaw_source, tart)
        if openclaw_payload:
            acquire_path_lock(OPENCLAW_PAYLOAD_LOCK, vm_name, openclaw_payload, tart)
        if signal_payload:
            acquire_path_lock(SIGNAL_PAYLOAD_LOCK, vm_name, signal_payload, tart)
    except LockError as exc:
        raise UserFacingError(str(exc)) from exc


def _mutagen_key_path(vm_name: str) -> Path:
    return STATE_DIR / "mutagen" / "keys" / vm_name / "id_ed25519"


def _ensure_mutagen_keypair(vm_name: str) -> Path:
    key_path = _mutagen_key_path(vm_name)
    pub_path = key_path.with_suffix(".pub")
    if key_path.exists() and pub_path.exists():
        return key_path

    key_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ssh-keygen",
        "-q",
        "-t",
        "ed25519",
        "-N",
        "",
        "-f",
        str(key_path),
    ]
    try:
        proc = subprocess.run(cmd, check=False, text=True, capture_output=True)
    except FileNotFoundError as exc:
        raise UserFacingError("Error: Command not found: ssh-keygen") from exc
    if proc.returncode != 0:
        details = (proc.stderr or proc.stdout or "").strip()
        detail_suffix = f"\n{details}" if details else ""
        raise UserFacingError(f"Error: Could not generate Mutagen SSH key for '{vm_name}'.{detail_suffix}")
    return key_path


def _resolve_mutagen_auth(vm_name: str, vm_ip: str, *, auth_mode: MutagenAuthMode) -> tuple[str, str]:
    if auth_mode == "bootstrap_admin":
        user, password = BOOTSTRAP_ADMIN_USER, BOOTSTRAP_ADMIN_PASSWORD
    else:
        try:
            user, password = vm_user_credentials(vm_name, secrets_file=SECRETS_FILE)
        except FileNotFoundError as exc:
            raise UserFacingError(missing_secrets_message(SECRETS_FILE)) from exc
        except OSError as exc:
            raise UserFacingError(f"Error: Could not read secrets file '{SECRETS_FILE}': {exc}") from exc
        except ValueError as exc:
            raise UserFacingError(str(exc)) from exc

    probe_cmd = "true"
    print(f"    trying SSH auth as '{user}'...")
    probe = _ansible_shell(
        vm_ip,
        probe_cmd,
        ansible_user=user,
        ansible_password=password,
        become=False,
        inventory_path=f"{vm_ip},",
    )
    if probe.returncode == 0:
        return user, password

    details = (probe.stderr or probe.stdout or "").strip()
    detail_lines = f"\n{details}" if details else ""
    raise UserFacingError(
        "Error: Could not establish guest SSH credentials for Mutagen sync setup.\n"
        f"  attempted user: {user}{detail_lines}"
    )


def _ensure_remote_mutagen_authorized_key(
    vm_name: str, vm_ip: str, *, ansible_user: str, ansible_password: str
) -> None:
    key_path = _ensure_mutagen_keypair(vm_name)
    pub_key = key_path.with_suffix(".pub").read_text(encoding="utf-8").strip()

    cmd = (
        "set -e; "
        "mkdir -p ~/.ssh; chmod 700 ~/.ssh; "
        "touch ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys; "
        f"grep -qxF {shlex.quote(pub_key)} ~/.ssh/authorized_keys || "
        f"printf '%s\\n' {shlex.quote(pub_key)} >> ~/.ssh/authorized_keys"
    )
    proc = _ansible_shell(
        vm_ip,
        cmd,
        ansible_user=ansible_user,
        ansible_password=ansible_password,
        become=False,
        inventory_path=f"{vm_ip},",
    )
    if proc.returncode != 0:
        details = (proc.stderr or proc.stdout or "").strip()
        raise UserFacingError(
            "Error: Could not install Mutagen SSH key in guest user authorized_keys.\n"
            f"{details}"
        )


def _prepare_remote_mutagen_targets(
    vm_ip: str,
    specs: Sequence[SessionSpec],
    *,
    ansible_user: str,
    ansible_password: str,
) -> None:
    clauses: list[str] = ["set -e"]
    for spec in specs:
        guest = shlex.quote(spec.guest_path)
        clauses.append(f"path={guest}")
        clauses.append('if [ -L "$path" ]; then rm "$path"; fi')
        clauses.append('mkdir -p "$path"')
        clauses.append('chmod -R a+rwX "$path"')
    cmd = "; ".join(clauses)

    proc = _ansible_shell(
        vm_ip,
        cmd,
        ansible_user=ansible_user,
        ansible_password=ansible_password,
        become=False,
        inventory_path=f"{vm_ip},",
    )
    if proc.returncode != 0:
        details = (proc.stderr or proc.stdout or "").strip()
        raise UserFacingError(
            "Error: Could not prepare guest directories for Mutagen sync.\n"
            f"{details}"
        )


def _wait_for_mutagen_sync_ready(
    vm_ip: str,
    specs: Sequence[SessionSpec],
    *,
    vm_name: str = "",
    ansible_user: str,
    ansible_password: str,
    timeout_seconds: int,
) -> list[str]:
    if not specs:
        return []

    marker_token = f"{int(time.time())}-{os.getpid()}"
    marker_paths: dict[str, tuple[Path, bool]] = {}
    for spec in specs:
        marker_name = f".clawbox-sync-ready-{spec.kind}-{marker_token}"
        host_marker = spec.host_path / marker_name
        guest_marker = f"{spec.guest_path}/{marker_name}"
        try:
            host_marker.write_text("ready\n", encoding="utf-8")
        except OSError as exc:
            raise UserFacingError(
                "Error: Could not write sync readiness marker on host path.\n"
                f"  path: {host_marker}\n"
                f"{exc}"
            ) from exc
        marker_paths[guest_marker] = (host_marker, spec.ready_required)

    try:
        waited = 0
        remote_paths = list(marker_paths.keys())
        required_remote_paths = [path for path, (_, required) in marker_paths.items() if required]
        optional_remote_paths = [path for path in remote_paths if path not in required_remote_paths]
        last_statuses = {path: "missing" for path in remote_paths}
        last_error = ""
        check_clauses = [
            f"if [ -f {shlex.quote(path)} ]; then printf '%s=%s\\n' {shlex.quote(path)} ok; "
            f"else printf '%s=%s\\n' {shlex.quote(path)} missing; missing=1; fi"
            for path in remote_paths
        ]
        check_cmd = "missing=0; " + "; ".join(check_clauses) + "; exit $missing"
        while waited < timeout_seconds:
            probe = _ansible_shell(
                vm_ip,
                check_cmd,
                ansible_user=ansible_user,
                ansible_password=ansible_password,
                become=False,
                inventory_path=f"{vm_ip},",
            )
            last_statuses = _parse_mount_statuses(probe.stdout, remote_paths)
            last_error = (probe.stderr or probe.stdout or "").strip()
            if all(last_statuses.get(path) == "ok" for path in required_remote_paths):
                return [path for path in optional_remote_paths if last_statuses.get(path) != "ok"]
            time.sleep(2)
            waited += 2
    finally:
        for host_marker, _required in marker_paths.values():
            host_marker.unlink(missing_ok=True)

    status_lines = _format_mount_statuses(last_statuses)
    hint_lines = [
        "Error: Mutagen sync did not become ready before timeout.",
        f"  vm ip: {vm_ip}",
        f"  timeout: {timeout_seconds}s",
        "  marker visibility checks:",
        status_lines,
    ]
    if last_error:
        hint_lines += ["  last probe output:", f"    {last_error}"]
    if vm_name:
        hint_lines.append("  mutagen session diagnostics:")
        try:
            diagnostics = vm_sessions_status(vm_name)
        except MutagenError as exc:
            diagnostics = f"Error: Could not collect mutagen session diagnostics.\n{exc}"
        if diagnostics:
            hint_lines.extend([f"    {line}" for line in diagnostics.splitlines()])
    hint_lines += ["Retry launch/provision after VM is fully responsive."]
    raise UserFacingError("\n".join(hint_lines))


def _activate_mutagen_sync(
    *,
    vm_name: str,
    openclaw_source: str,
    openclaw_payload: str,
    signal_payload: str,
    tart: TartClient,
    auth_mode: MutagenAuthMode,
    timeout_seconds: int = 120,
    reason: str = "unspecified",
) -> None:
    prep_started = time.monotonic()
    emit_sync_event(
        STATE_DIR,
        vm_name,
        event="activate_start",
        actor="orchestrator",
        reason=reason,
        details={"signal_payload_enabled": bool(signal_payload), "auth_mode": auth_mode},
    )
    try:
        if not mutagen_available():
            raise UserFacingError("Error: Command not found: mutagen")
        host_paths = _host_paths_from_args(openclaw_source, openclaw_payload, signal_payload)
        specs = _build_sync_specs(vm_name, host_paths)
        _validate_dirs([str(spec.host_path) for spec in specs])

        if not tart.vm_running(vm_name):
            raise UserFacingError(
                f"Error: VM '{vm_name}' must be running before activating Mutagen sync.\n"
                "Start the VM and retry."
            )
        print("Preparing Mutagen sync...")
        print(f"  waiting for VM IP (timeout: {timeout_seconds}s)...")
        vm_ip = _resolve_vm_ip(tart, vm_name, timeout_seconds)
        print(f"  vm ip: {vm_ip}")
        print("  checking guest SSH credentials for sync...")
        mutagen_user, mutagen_password = _resolve_mutagen_auth(vm_name, vm_ip, auth_mode=auth_mode)
        print(f"  using guest account: {mutagen_user}")
        print("  ensuring guest authorized_keys includes Clawbox sync key...")
        _ensure_remote_mutagen_authorized_key(
            vm_name,
            vm_ip,
            ansible_user=mutagen_user,
            ansible_password=mutagen_password,
        )
        key_path = _ensure_mutagen_keypair(vm_name)
        print("  preparing guest sync directories...")
        _prepare_remote_mutagen_targets(
            vm_ip,
            specs,
            ansible_user=mutagen_user,
            ansible_password=mutagen_password,
        )
        print("  creating Mutagen sessions and waiting for initial sync...")
        alias = ensure_mutagen_ssh_alias(vm_name, vm_ip, mutagen_user, key_path)

        session_sync_started = time.monotonic()
        ensure_vm_sessions(vm_name, alias, specs)
        session_sync_elapsed = time.monotonic() - session_sync_started
        print(f"  session create/flush elapsed: {session_sync_elapsed:.1f}s")

        print(f"  verifying sync-ready marker visibility (timeout: {MUTAGEN_READY_TIMEOUT_SECONDS}s)...")
        ready_started = time.monotonic()
        optional_pending = _wait_for_mutagen_sync_ready(
            vm_ip,
            specs,
            vm_name=vm_name,
            ansible_user=mutagen_user,
            ansible_password=mutagen_password,
            timeout_seconds=MUTAGEN_READY_TIMEOUT_SECONDS,
        )
        ready_elapsed = time.monotonic() - ready_started
        print(f"  sync-ready marker visibility elapsed: {ready_elapsed:.1f}s")
        if optional_pending:
            print("  optional sync paths still warming up (continuing):")
            for path in optional_pending:
                print(f"    - {path}")
        mark_mutagen_vm_active(STATE_DIR, vm_name)
        prep_elapsed = time.monotonic() - prep_started
        print(f"  mutagen preparation elapsed: {prep_elapsed:.1f}s")
        print("Mutagen sync active (bidirectional):")
        for spec in specs:
            print(f"  {spec.kind}: {spec.host_path} <-> {spec.guest_path}")
        emit_sync_event(
            STATE_DIR,
            vm_name,
            event="activate_ok",
            actor="orchestrator",
            reason=reason,
            details={"session_spec_count": len(specs), "elapsed_seconds": round(prep_elapsed, 1)},
        )
    except Exception as exc:
        emit_sync_event(
            STATE_DIR,
            vm_name,
            event="activate_error",
            actor="orchestrator",
            reason=reason,
            details={"error_type": type(exc).__name__, "error": str(exc)},
        )
        if isinstance(exc, MutagenError):
            raise UserFacingError(str(exc)) from exc
        raise


def _activate_mutagen_sync_from_locks(
    vm_name: str, tart: TartClient, *, reason: str = "activate_from_locks"
) -> None:
    locked_paths = _host_paths_from_locks(vm_name)
    source_path = locked_paths.get(OPENCLAW_SOURCE_LOCK, "")
    payload_path = locked_paths.get(OPENCLAW_PAYLOAD_LOCK, "")
    signal_path = locked_paths.get(SIGNAL_PAYLOAD_LOCK, "")
    if not source_path or not payload_path:
        raise UserFacingError(
            "Error: Could not determine developer source/payload host paths for Mutagen sync.\n"
            "Launch with --openclaw-source and --openclaw-payload first."
        )
    _activate_mutagen_sync(
        vm_name=vm_name,
        openclaw_source=source_path,
        openclaw_payload=payload_path,
        signal_payload=signal_path,
        tart=tart,
        auth_mode="bootstrap_admin",
        reason=reason,
    )


def _deactivate_mutagen_sync(vm_name: str, *, flush: bool, reason: str = "unspecified") -> None:
    emit_sync_event(
        STATE_DIR,
        vm_name,
        event="teardown_start",
        actor="orchestrator",
        reason=reason,
        details={"flush": flush},
    )
    try:
        teardown_vm_sync(STATE_DIR, vm_name, flush=flush)
    except MutagenError as exc:
        emit_sync_event(
            STATE_DIR,
            vm_name,
            event="teardown_error",
            actor="orchestrator",
            reason=reason,
            details={"flush": flush, "error_type": type(exc).__name__, "error": str(exc)},
        )
        raise UserFacingError(str(exc)) from exc
    emit_sync_event(
        STATE_DIR,
        vm_name,
        event="teardown_ok",
        actor="orchestrator",
        reason=reason,
        details={"flush": flush},
    )


def create_vm(vm_number: int, tart: TartClient) -> None:
    vm_name = vm_name_for(vm_number)
    if tart.vm_exists(vm_name):
        raise UserFacingError(
            f"Error: VM '{vm_name}' already exists. Delete it first with: "
            f"clawbox delete {vm_number}"
        )

    try:
        tart.clone(BASE_IMAGE, vm_name)
    except TartError as exc:
        raise UserFacingError(
            _with_virtualization_limit_hint(
                f"Error: Failed to create VM '{vm_name}' from base image '{BASE_IMAGE}'.\n{exc}"
            )
        ) from exc
    print(f"Created VM: {vm_name}")


def launch_vm(
    vm_number: int,
    profile: str,
    openclaw_source: str,
    openclaw_payload: str,
    signal_payload: str,
    headless: bool,
    tart: TartClient,
) -> None:
    _validate_profile(profile)
    _validate_profile_mount_args(profile, openclaw_source, openclaw_payload, signal_payload)
    vm_name = vm_name_for(vm_number)

    if not tart.vm_exists(vm_name):
        raise UserFacingError(
            f"Error: VM '{vm_name}' does not exist. Create it first with: "
            f"clawbox create {vm_number}"
        )
    if profile == "developer" and not mutagen_available():
        raise UserFacingError("Error: Command not found: mutagen")
    marker_file = STATE_DIR / f"{vm_name}.provisioned"
    launch_sync_auth_mode: MutagenAuthMode = (
        "vm_user" if marker_file.exists() else "bootstrap_admin"
    )

    if tart.vm_running(vm_name):
        print(f"VM '{vm_name}' is already running.")
        if profile == "developer":
            _validate_dirs([openclaw_source, openclaw_payload, signal_payload])
            _acquire_locks(tart, vm_name, openclaw_source, openclaw_payload, signal_payload)
            if signal_payload:
                _ensure_signal_payload_host_marker(signal_payload, vm_name)
        try:
            watcher_pid = start_vm_watcher(STATE_DIR, vm_name)
        except WatcherError as exc:
            raise UserFacingError(str(exc)) from exc
        print(f"Watcher active (PID {watcher_pid}).")
        if profile == "developer":
            _activate_mutagen_sync(
                vm_name=vm_name,
                openclaw_source=openclaw_source,
                openclaw_payload=openclaw_payload,
                signal_payload=signal_payload,
                tart=tart,
                auth_mode=launch_sync_auth_mode,
                reason="launch_vm_already_running",
            )
        return

    _validate_dirs([openclaw_source, openclaw_payload, signal_payload])
    _acquire_locks(tart, vm_name, openclaw_source, openclaw_payload, signal_payload)
    if signal_payload:
        _ensure_signal_payload_host_marker(signal_payload, vm_name)

    run_args: list[str] = []
    if headless:
        run_args.append("--no-graphics")

    print(f"Launching {vm_name} (profile: {profile})...")
    if profile == "developer":
        print(f"  --openclaw-source     {openclaw_source}")
        print(f"  --openclaw-payload    {openclaw_payload}")
    if signal_payload:
        print(f"  --signal-cli-payload  {signal_payload}")
    if headless:
        print("  launch mode:          headless")
    print("")

    launch_log_file = STATE_DIR / "logs" / f"{vm_name}.launch.log"
    try:
        proc = tart.run_in_background(vm_name, run_args, launch_log_file)
    except TartError as exc:
        raise UserFacingError(
            _with_virtualization_limit_hint(f"Error: Failed to launch VM '{vm_name}'.\n{exc}")
        ) from exc
    time.sleep(1)

    if proc.poll() is not None:
        msg = [f"Error: tart run exited before '{vm_name}' reached a running state."]
        tail = tail_lines(launch_log_file)
        if tail:
            msg.append(f"Recent tart output ({launch_log_file}):")
            msg.append(tail)
        raise UserFacingError(_with_virtualization_limit_hint("\n".join(msg)))

    if not wait_for_vm_running(tart, vm_name, timeout_seconds=30):
        msg = [
            f"Error: '{vm_name}' did not enter running state within 30s.",
            f"tart output log: {launch_log_file}",
        ]
        tail = tail_lines(launch_log_file)
        if tail:
            msg.append(tail)
        raise UserFacingError(_with_virtualization_limit_hint("\n".join(msg)))

    print(f"VM started in background (PID {proc.pid}).")
    try:
        watcher_pid = start_vm_watcher(STATE_DIR, vm_name)
    except WatcherError as exc:
        raise UserFacingError(str(exc)) from exc
    print(f"Watcher active (PID {watcher_pid}).")
    if profile == "developer":
        _activate_mutagen_sync(
            vm_name=vm_name,
            openclaw_source=openclaw_source,
            openclaw_payload=openclaw_payload,
            signal_payload=signal_payload,
            tart=tart,
            auth_mode=launch_sync_auth_mode,
            reason="launch_vm_after_start",
        )


@dataclass
class ProvisionOptions:
    vm_number: int
    profile: str
    enable_playwright: bool
    enable_tailscale: bool
    enable_signal_cli: bool
    enable_signal_payload: bool
    skip_sync_activation: bool = False


def _resolve_vm_ip(tart: TartClient, vm_name: str, timeout_seconds: int) -> str:
    waited = 0
    while waited < timeout_seconds:
        ip = tart.ip(vm_name)
        if ip:
            return ip
        time.sleep(2)
        waited += 2
    raise UserFacingError(
        f"Error: Timed out waiting for '{vm_name}' to report an IP address.\n"
        "Ensure the VM is running and fully booted, then retry."
    )


def _remote_shell_context() -> RemoteShellContext:
    return RemoteShellContext(
        ansible_dir=ANSIBLE_DIR,
        connect_timeout_seconds=ANSIBLE_CONNECT_TIMEOUT_SECONDS,
        command_timeout_seconds=ANSIBLE_COMMAND_TIMEOUT_SECONDS,
    )


def _require_vm_exists(tart: TartClient, vm_name: str, vm_number: int) -> None:
    if tart.vm_exists(vm_name):
        return
    raise UserFacingError(
        f"Error: VM '{vm_name}' does not exist.\n"
        f"Create it first with: clawbox create {vm_number}"
    )


def _require_vm_running(tart: TartClient, vm_name: str, vm_number: int) -> None:
    if tart.vm_running(vm_name):
        return
    raise UserFacingError(
        f"Error: VM '{vm_name}' is not running.\n"
        f"Start it first with: clawbox launch {vm_number}"
    )


def _run_remote_path_probe(
    vm_name: str,
    *,
    shell_cmd: str,
    paths: Sequence[str],
    inventory_path: str = "inventory/tart_inventory.py",
    target_host: str | None = None,
) -> tuple[int, dict[str, str], str]:
    ansible_target = target_host or vm_name
    return run_remote_path_probe_shared(
        ansible_target,
        shell_cmd=shell_cmd,
        paths=paths,
        ansible_user=BOOTSTRAP_ADMIN_USER,
        ansible_password=BOOTSTRAP_ADMIN_PASSWORD,
        parse_statuses=_parse_mount_statuses,
        become=False,
        inventory_path=inventory_path,
        shell_runner=_ansible_shell,
    )


def _wait_for_remote_probe(
    vm_name: str,
    *,
    shell_cmd: str,
    paths: Sequence[str],
    timeout_seconds: int,
    is_success: Callable[[int, dict[str, str]], bool],
    inventory_path: str = "inventory/tart_inventory.py",
    target_host: str | None = None,
) -> tuple[bool, dict[str, str], str]:
    ansible_target = target_host or vm_name
    return wait_for_remote_probe_shared(
        ansible_target,
        shell_cmd=shell_cmd,
        paths=paths,
        ansible_user=BOOTSTRAP_ADMIN_USER,
        ansible_password=BOOTSTRAP_ADMIN_PASSWORD,
        parse_statuses=_parse_mount_statuses,
        is_success=is_success,
        timeout_seconds=timeout_seconds,
        become=False,
        inventory_path=inventory_path,
        shell_runner=_ansible_shell,
    )


def _ansible_shell(
    vm_name: str,
    shell_cmd: str,
    *,
    ansible_user: str,
    ansible_password: str,
    become: bool = False,
    inventory_path: str = "inventory/tart_inventory.py",
) -> subprocess.CompletedProcess[str]:
    return ansible_shell_shared(
        vm_name,
        shell_cmd,
        ansible_user=ansible_user,
        ansible_password=ansible_password,
        become=become,
        context=_remote_shell_context(),
        inventory_path=inventory_path,
    )


def _mount_status_command(mount_paths: Sequence[str]) -> str:
    return build_mount_status_command(mount_paths)


def _parse_mount_statuses(stdout: str, mount_paths: Sequence[str]) -> dict[str, str]:
    return parse_mount_statuses(stdout, mount_paths)


def _format_mount_statuses(statuses: dict[str, str]) -> str:
    return format_mount_statuses(statuses)


def _preflight_developer_mounts(
    vm_name: str,
    *,
    vm_number: int,
    openclaw_payload_host: str,
    signal_payload_host: str,
    include_signal_payload: bool,
    timeout_seconds: int,
) -> None:
    mount_paths = [OPENCLAW_SOURCE_MOUNT, OPENCLAW_PAYLOAD_MOUNT]
    if include_signal_payload:
        mount_paths.append(SIGNAL_PAYLOAD_MOUNT)

    print("  verifying synced developer paths...")
    payload_probe_name = ""
    signal_probe_name = ""
    payload_probe_path: Path | None = None
    signal_probe_path: Path | None = None
    required_files: list[str] = []
    last_checks: dict[str, str] = {}
    last_mounts = {path: "unknown" for path in mount_paths}
    last_error = ""
    try:
        payload_probe_name = f".clawbox-mount-probe-{int(time.time())}-{os.getpid()}-payload"
        payload_probe_path = Path(openclaw_payload_host) / payload_probe_name
        payload_probe_path.write_text("probe\n", encoding="utf-8")

        if include_signal_payload and signal_payload_host:
            signal_probe_name = f".clawbox-mount-probe-{int(time.time())}-{os.getpid()}-signal"
            signal_probe_path = Path(signal_payload_host) / signal_probe_name
            signal_probe_path.write_text("probe\n", encoding="utf-8")

        required_files = [
            f"{OPENCLAW_SOURCE_MOUNT}/package.json",
            f"{OPENCLAW_PAYLOAD_MOUNT}/{payload_probe_name}",
        ]
        if include_signal_payload:
            required_files.append(f"{SIGNAL_PAYLOAD_MOUNT}/{signal_probe_name}")

        check_clauses = [
            f"if [ -f {shlex.quote(path)} ]; then printf '%s=%s\\n' {shlex.quote(path)} ok; "
            f"else printf '%s=%s\\n' {shlex.quote(path)} missing; missing=1; fi"
            for path in required_files
        ]
        checks_cmd = "missing=0; " + "; ".join(check_clauses) + "; exit $missing"
        succeeded, last_checks, last_error = _wait_for_remote_probe(
            vm_name,
            shell_cmd=checks_cmd,
            paths=required_files,
            timeout_seconds=timeout_seconds,
            is_success=lambda returncode, statuses: returncode == 0
            and all(status == "ok" for status in statuses.values()),
        )
        if succeeded:
            print("  synced developer paths verified.")
            return

        mount_cmd = _mount_status_command(mount_paths)
        mount_returncode, mount_statuses, _ = _run_remote_path_probe(
            vm_name,
            shell_cmd=mount_cmd,
            paths=mount_paths,
        )
        if mount_returncode == 0:
            last_mounts = mount_statuses
    finally:
        if payload_probe_path is not None:
            payload_probe_path.unlink(missing_ok=True)
        if signal_probe_path is not None:
            signal_probe_path.unlink(missing_ok=True)

    check_details = _format_mount_statuses(last_checks)
    mount_details = _format_mount_statuses(last_mounts)
    hint_lines = [
        "Error: Required synced developer paths failed preflight checks in the guest.",
        "Clawbox requires visible synced content before provisioning in developer mode.",
        f"  vm: {vm_name}",
        f"  timeout: {timeout_seconds}s",
        "  file visibility checks:",
        check_details,
        "  mount command diagnostics:",
        mount_details,
    ]
    if last_error:
        hint_lines += ["  last probe output:", f"    {last_error}"]
    hint_lines += [
        "Rerun with a fresh VM if needed:",
        f"  clawbox delete {vm_number}",
        f"  clawbox up {vm_number} --developer ...",
    ]
    raise UserFacingError("\n".join(hint_lines))


def _preflight_signal_payload_marker(
    vm_name: str,
    *,
    vm_number: int,
    timeout_seconds: int,
    inventory_path: str = "inventory/tart_inventory.py",
    target_host: str | None = None,
) -> None:
    marker_path = f"{SIGNAL_PAYLOAD_MOUNT}/{SIGNAL_PAYLOAD_MARKER_FILENAME}"
    print("  verifying signal-cli payload marker visibility...")

    check_cmd = (
        f"if [ -f {shlex.quote(marker_path)} ]; then "
        f"printf '%s=%s\\n' {shlex.quote(marker_path)} ok; "
        f"exit 0; "
        f"else printf '%s=%s\\n' {shlex.quote(marker_path)} missing; exit 1; fi"
    )
    succeeded, last_statuses, last_error = _wait_for_remote_probe(
        vm_name,
        shell_cmd=check_cmd,
        paths=[marker_path],
        timeout_seconds=timeout_seconds,
        is_success=lambda returncode, statuses: returncode == 0
        and statuses.get(marker_path) == "ok",
        inventory_path=inventory_path,
        target_host=target_host,
    )
    last_status = last_statuses.get(marker_path, "unknown")
    if succeeded:
        print("  signal-cli payload marker verified.")
        return

    hint_lines = [
        "Error: signal-cli payload marker was not visible in the guest.",
        "This safety check prevents destructive payload seeding from an unmounted/wrong directory.",
        f"  vm: {vm_name}",
        f"  expected marker: {marker_path}",
        f"  timeout: {timeout_seconds}s",
        f"  last marker status: {last_status}",
    ]
    if last_error:
        hint_lines += ["  last probe output:", f"    {last_error}"]
    hint_lines += [
        "Retry with a fresh launch and then provision:",
        f"  clawbox launch {vm_number} --developer --signal-cli-payload <path> ...",
        f"  clawbox provision {vm_number} --developer --add-signal-cli-provisioning --enable-signal-payload",
    ]
    raise UserFacingError("\n".join(hint_lines))


def provision_vm(opts: ProvisionOptions, tart: TartClient) -> None:
    _validate_profile(opts.profile)
    _validate_feature_flags(
        opts.profile,
        enable_playwright=opts.enable_playwright,
        enable_tailscale=opts.enable_tailscale,
        enable_signal_cli=opts.enable_signal_cli,
        enable_signal_payload=opts.enable_signal_payload,
        signal_payload="",
    )

    ensure_secrets_file(create_if_missing=False)
    vm_name = vm_name_for(opts.vm_number)
    _require_vm_exists(tart, vm_name, opts.vm_number)
    _require_vm_running(tart, vm_name, opts.vm_number)
    boot_timeout = int(os.getenv("VM_BOOT_TIMEOUT_SECONDS", "300"))

    print(f"Provisioning {vm_name}...")
    print(f"  profile: {opts.profile}")
    print(f"  playwright enabled: {str(opts.enable_playwright).lower()}")
    print(f"  tailscale enabled: {str(opts.enable_tailscale).lower()}")
    print(f"  signal-cli enabled: {str(opts.enable_signal_cli).lower()}")
    print(f"  signal payload enabled: {str(opts.enable_signal_payload).lower()}")
    print(f"  waiting for VM IP (timeout: {boot_timeout}s; resolver: agent->default)...")
    vm_ip = _resolve_vm_ip(tart, vm_name, boot_timeout)
    print(f"  vm ip: {vm_ip}")
    inventory_path = f"{vm_ip},"
    if opts.profile == "developer" and not opts.skip_sync_activation:
        _activate_mutagen_sync_from_locks(vm_name, tart, reason="provision_vm")

    if opts.profile == "developer" and opts.enable_signal_payload:
        _preflight_signal_payload_marker(
            vm_name,
            vm_number=opts.vm_number,
            timeout_seconds=min(boot_timeout, 120),
            inventory_path=inventory_path,
            target_host=vm_ip,
        )

    enable_dev_mounts = opts.profile == "developer"
    playbook_cmd = [
        "ansible-playbook",
        "-i",
        inventory_path,
        "playbooks/provision.yml",
        "--extra-vars",
        f"@{SECRETS_FILE}",
        "--extra-vars",
        "ansible_become=true",
        "--extra-vars",
        f"vm_number={opts.vm_number}",
        "--extra-vars",
        f"clawbox_profile={opts.profile}",
        "--extra-vars",
        f"clawbox_enable_dev_mounts={'true' if enable_dev_mounts else 'false'}",
        "--extra-vars",
        f"clawbox_enable_playwright={'true' if opts.enable_playwright else 'false'}",
        "--extra-vars",
        f"clawbox_enable_tailscale={'true' if opts.enable_tailscale else 'false'}",
        "--extra-vars",
        f"clawbox_enable_signal_cli={'true' if opts.enable_signal_cli else 'false'}",
        "--extra-vars",
        f"clawbox_enable_signal_payload={'true' if opts.enable_signal_payload else 'false'}",
    ]
    try:
        proc = subprocess.run(playbook_cmd, cwd=ANSIBLE_DIR, check=False)
    except FileNotFoundError as exc:
        raise UserFacingError("Error: Command not found: ansible-playbook") from exc
    if proc.returncode != 0:
        raise UserFacingError("Provisioning failed.")

    marker = ProvisionMarker(
        vm_name=vm_name,
        profile=opts.profile,
        playwright=opts.enable_playwright,
        tailscale=opts.enable_tailscale,
        signal_cli=opts.enable_signal_cli,
        signal_payload=opts.enable_signal_payload,
        provisioned_at=current_utc_timestamp(),
        sync_backend=REQUIRED_DEVELOPER_SYNC_BACKEND if opts.profile == "developer" else "",
    )
    marker.write(STATE_DIR / f"{vm_name}.provisioned")
    print(f"Provisioning completed: {vm_name}")


@dataclass
class UpOptions:
    vm_number: int
    profile: str
    openclaw_source: str
    openclaw_payload: str
    signal_payload: str
    enable_playwright: bool
    enable_tailscale: bool
    enable_signal_cli: bool


def _stop_vm_and_wait(tart: TartClient, vm_name: str, timeout_seconds: int) -> bool:
    stop_vm_watcher(STATE_DIR, vm_name)
    _deactivate_mutagen_sync(vm_name, flush=True, reason="_stop_vm_and_wait")
    tart.stop(vm_name)
    waited = 0
    while waited < timeout_seconds:
        if not tart.vm_running(vm_name):
            return True
        time.sleep(2)
        waited += 2
    return not tart.vm_running(vm_name)


def _render_up_command(opts: UpOptions) -> str:
    cmd = ["clawbox", "up", str(opts.vm_number)]
    if opts.profile == "developer":
        cmd.append("--developer")
        cmd += ["--openclaw-source", opts.openclaw_source]
        cmd += ["--openclaw-payload", opts.openclaw_payload]
    enabled_services = enabled_optional_service_keys(
        enable_playwright=opts.enable_playwright,
        enable_tailscale=opts.enable_tailscale,
        enable_signal_cli=opts.enable_signal_cli,
    )
    for spec in OPTIONAL_SERVICES:
        if spec.key in enabled_services:
            cmd.append(spec.cli_flag)
    if opts.signal_payload:
        cmd += ["--signal-cli-payload", opts.signal_payload]
    return " ".join(shlex.quote(part) for part in cmd)


def _render_recreate_commands(opts: UpOptions) -> str:
    return "\n".join(
        [
            f"  clawbox delete {opts.vm_number}",
            f"  {_render_up_command(opts)}",
        ]
    )


def _compute_up_provision_reason(
    opts: UpOptions,
    marker_file: Path,
    created_vm: bool,
    desired_signal_payload: bool,
) -> str:
    if created_vm:
        return "VM was created in this run"
    if not marker_file.exists():
        raise UserFacingError(
            f"Error: Provision marker is missing for existing VM '{vm_name_for(opts.vm_number)}'.\n"
            "In-place reprovision is unsafe after initial provisioning.\n"
            "Recreate the VM instead:\n"
            f"{_render_recreate_commands(opts)}"
        )

    marker = ProvisionMarker.from_file(marker_file)
    if marker is None:
        raise UserFacingError(
            f"Error: Provision marker exists but could not be parsed: {marker_file}\n"
            "In-place reprovision is unsafe after initial provisioning.\n"
            "Recreate the VM instead:\n"
            f"{_render_recreate_commands(opts)}"
        )

    if (
        opts.profile == "developer"
        and marker.profile == "developer"
        and marker.sync_backend != REQUIRED_DEVELOPER_SYNC_BACKEND
    ):
        marker_backend = marker.sync_backend or "(missing)"
        raise UserFacingError(
            "Error: Existing developer VM uses a legacy provision marker format.\n"
            "This VM predates required sync backend metadata for developer mode.\n"
            f"  marker sync_backend: {marker_backend}\n"
            f"  required sync_backend: {REQUIRED_DEVELOPER_SYNC_BACKEND}\n"
            "In-place reprovision is unsafe after initial provisioning.\n"
            "Recreate the VM instead:\n"
            f"{_render_recreate_commands(opts)}"
        )

    if (
        marker.profile != opts.profile
        or marker.playwright != opts.enable_playwright
        or marker.tailscale != opts.enable_tailscale
        or marker.signal_cli != opts.enable_signal_cli
        or marker.signal_payload != desired_signal_payload
    ):
        raise UserFacingError(
            "Error: Requested options do not match this VM's existing provision marker.\n"
            "In-place reprovision is unsafe after initial provisioning.\n"
            f"  marker file: {marker_file}\n"
            "  marker profile/playwright/tailscale/signal_cli/signal_payload: "
            f"{marker.profile}/{str(marker.playwright).lower()}/"
            f"{str(marker.tailscale).lower()}/{str(marker.signal_cli).lower()}/"
            f"{str(marker.signal_payload).lower()}\n"
            "  requested profile/playwright/tailscale/signal_cli/signal_payload: "
            f"{opts.profile}/{str(opts.enable_playwright).lower()}/"
            f"{str(opts.enable_tailscale).lower()}/{str(opts.enable_signal_cli).lower()}/"
            f"{str(desired_signal_payload).lower()}\n"
            "Recreate the VM instead:\n"
            f"{_render_recreate_commands(opts)}"
        )
    return ""


def _ensure_vm_running_for_up(
    vm_name: str,
    opts: UpOptions,
    provision_reason: str,
    tart: TartClient,
) -> bool:
    if tart.vm_running(vm_name):
        print(f"VM '{vm_name}' is already running.")
        return False

    print(f"VM '{vm_name}' is not running; launching it...")
    launched_headless = bool(provision_reason)
    launch_vm(
        vm_number=opts.vm_number,
        profile=opts.profile,
        openclaw_source=opts.openclaw_source,
        openclaw_payload=opts.openclaw_payload,
        signal_payload=opts.signal_payload,
        headless=launched_headless,
        tart=tart,
    )
    if not wait_for_vm_running(tart, vm_name, timeout_seconds=60, poll_seconds=2):
        raise UserFacingError(
            f"Error: VM '{vm_name}' did not transition to running state after launch."
        )
    return launched_headless


def _relaunch_gui_after_headless_provision(
    vm_name: str,
    opts: UpOptions,
    tart: TartClient,
    launched_headless: bool,
) -> None:
    if not launched_headless:
        return

    print(f"Provisioning completed; relaunching '{vm_name}' with a Tart window...")
    if opts.profile == "developer":
        print("  Note: the VM window may appear before host<->VM sync is ready.")
        print("  Wait for 'Clawbox is ready:' before logging in or editing synced files.")
    if tart.vm_running(vm_name) and not _stop_vm_and_wait(tart, vm_name, 120):
        raise UserFacingError(
            f"Error: Timed out stopping headless VM '{vm_name}' before GUI relaunch.\n"
            f"Try: clawbox down {opts.vm_number}"
        )
    launch_vm(
        vm_number=opts.vm_number,
        profile=opts.profile,
        openclaw_source=opts.openclaw_source,
        openclaw_payload=opts.openclaw_payload,
        signal_payload=opts.signal_payload,
        headless=False,
        tart=tart,
    )
    if not wait_for_vm_running(tart, vm_name, timeout_seconds=60, poll_seconds=2):
        raise UserFacingError(
            f"Error: VM '{vm_name}' did not transition to running state after GUI relaunch.\n"
            f"Try: clawbox launch {opts.vm_number}"
        )


def _ensure_running_after_provision_if_needed(
    vm_name: str,
    opts: UpOptions,
    tart: TartClient,
    provision_ran: bool,
) -> None:
    if not provision_ran or tart.vm_running(vm_name):
        return

    if not wait_for_vm_running(tart, vm_name, timeout_seconds=30, poll_seconds=2):
        print(f"VM '{vm_name}' is not running after provisioning; launching it...")
        launch_vm(
            vm_number=opts.vm_number,
            profile=opts.profile,
            openclaw_source=opts.openclaw_source,
            openclaw_payload=opts.openclaw_payload,
            signal_payload=opts.signal_payload,
            headless=False,
            tart=tart,
        )
        if not wait_for_vm_running(tart, vm_name, timeout_seconds=120, poll_seconds=2):
            raise UserFacingError(
                f"Error: VM '{vm_name}' did not return to running state after provisioning.\n"
                "Rerun:\n"
                f"  {_render_up_command(opts)}"
            )


def up(opts: UpOptions, tart: TartClient) -> None:
    _validate_profile(opts.profile)
    _validate_profile_mount_args(
        opts.profile, opts.openclaw_source, opts.openclaw_payload, opts.signal_payload
    )
    _validate_feature_flags(
        opts.profile,
        enable_playwright=opts.enable_playwright,
        enable_tailscale=opts.enable_tailscale,
        enable_signal_cli=opts.enable_signal_cli,
        enable_signal_payload=bool(opts.signal_payload),
        signal_payload=opts.signal_payload,
    )

    _validate_dirs([opts.openclaw_source, opts.openclaw_payload, opts.signal_payload])
    vm_name = vm_name_for(opts.vm_number)
    marker_file = STATE_DIR / f"{vm_name}.provisioned"
    desired_signal_payload = bool(opts.signal_payload)

    ensure_secrets_file(create_if_missing=True)

    created_vm = False
    if not tart.vm_exists(vm_name):
        print(f"VM '{vm_name}' does not exist; creating it...")
        create_vm(opts.vm_number, tart)
        created_vm = True
        if not tart.vm_exists(vm_name):
            raise UserFacingError(
                f"Error: VM '{vm_name}' was not found after create_vm completed.\n"
                "Check tart output and verify the base image exists: macos-base"
            )

    was_running_at_start = tart.vm_running(vm_name)
    provision_reason = _compute_up_provision_reason(
        opts,
        marker_file,
        created_vm,
        desired_signal_payload,
    )

    launched_headless = _ensure_vm_running_for_up(vm_name, opts, provision_reason, tart)

    provision_ran = False
    if provision_reason:
        print(f"Provisioning is required for '{vm_name}' ({provision_reason}).")
        if opts.profile == "developer":
            boot_timeout = _env_int("VM_BOOT_TIMEOUT_SECONDS", 300)
            _preflight_developer_mounts(
                vm_name,
                vm_number=opts.vm_number,
                openclaw_payload_host=opts.openclaw_payload,
                signal_payload_host=opts.signal_payload,
                include_signal_payload=bool(opts.signal_payload),
                timeout_seconds=min(boot_timeout, 120),
            )
        provision_vm(
            ProvisionOptions(
                vm_number=opts.vm_number,
                profile=opts.profile,
                enable_playwright=opts.enable_playwright,
                enable_tailscale=opts.enable_tailscale,
                enable_signal_cli=opts.enable_signal_cli,
                enable_signal_payload=desired_signal_payload,
                skip_sync_activation=opts.profile == "developer",
            ),
            tart,
        )
        provision_ran = True

        _relaunch_gui_after_headless_provision(vm_name, opts, tart, launched_headless)
    else:
        print(f"Provision marker found for '{vm_name}'; skipping provisioning.")
        print("  If this VM is not actually provisioned, recreate it with:")
        print(_render_recreate_commands(opts))

    print("")
    _ensure_running_after_provision_if_needed(vm_name, opts, tart, provision_ran)
    should_activate_mutagen = opts.profile == "developer" and was_running_at_start and not provision_ran
    if should_activate_mutagen:
        _activate_mutagen_sync(
            vm_name=vm_name,
            openclaw_source=opts.openclaw_source,
            openclaw_payload=opts.openclaw_payload,
            signal_payload=opts.signal_payload,
            tart=tart,
            auth_mode="vm_user",
            reason="up_existing_running_vm",
        )

    if tart.vm_running(vm_name):
        if provision_ran:
            print(f"Clawbox is ready: {vm_name}")
        else:
            print(f"Clawbox is running: {vm_name} (provisioning skipped)")
        return

    raise UserFacingError(
        f"Error: VM '{vm_name}' is not running after orchestration.\n"
        "Rerun:\n"
        f"  {_render_up_command(opts)}"
    )


def recreate(opts: UpOptions, tart: TartClient) -> None:
    vm_name = vm_name_for(opts.vm_number)
    print(f"Clean recreate requested for '{vm_name}'.")
    if tart.vm_exists(vm_name):
        down_vm(opts.vm_number, tart)
    delete_vm(opts.vm_number, tart)
    up(opts, tart)


def _wait_for_vm_absent(tart: TartClient, vm_name: str, timeout_seconds: int) -> bool:
    waited = 0
    while waited < timeout_seconds:
        if not tart.vm_exists(vm_name):
            return True
        time.sleep(2)
        waited += 2
    return not tart.vm_exists(vm_name)


def down_vm(vm_number: int, tart: TartClient) -> None:
    vm_name = vm_name_for(vm_number)
    if not tart.vm_exists(vm_name):
        stop_vm_watcher(STATE_DIR, vm_name)
        _deactivate_mutagen_sync(vm_name, flush=False, reason="down_vm_missing")
        cleanup_locks_for_vm(vm_name)
        print(f"VM '{vm_name}' does not exist.")
        return

    if tart.vm_running(vm_name):
        print(f"Stopping VM '{vm_name}'...")
        if not _stop_vm_and_wait(tart, vm_name, timeout_seconds=120):
            raise UserFacingError(
                f"Error: Timed out waiting for VM '{vm_name}' to stop.\n"
                f"Try again: clawbox down {vm_number}"
            )
        print(f"VM '{vm_name}' stopped.")
    else:
        print(f"VM '{vm_name}' is already stopped.")

    stop_vm_watcher(STATE_DIR, vm_name)
    _deactivate_mutagen_sync(vm_name, flush=False, reason="down_vm")
    cleanup_locks_for_vm(vm_name)


def delete_vm(vm_number: int, tart: TartClient) -> None:
    vm_name = vm_name_for(vm_number)
    marker_file = STATE_DIR / f"{vm_name}.provisioned"

    if not tart.vm_exists(vm_name):
        stop_vm_watcher(STATE_DIR, vm_name)
        _deactivate_mutagen_sync(vm_name, flush=False, reason="delete_vm_missing")
        marker_file.unlink(missing_ok=True)
        cleanup_locks_for_vm(vm_name)
        print(f"VM '{vm_name}' does not exist.")
        return

    if tart.vm_running(vm_name):
        print(f"Stopping VM '{vm_name}' before delete...")
        if not _stop_vm_and_wait(tart, vm_name, timeout_seconds=120):
            raise UserFacingError(
                f"Error: Timed out waiting for VM '{vm_name}' to stop before deletion.\n"
                f"Try again: clawbox delete {vm_number}"
            )

    print(f"Deleting VM '{vm_name}'...")
    tart.delete(vm_name)
    if not _wait_for_vm_absent(tart, vm_name, timeout_seconds=120):
        raise UserFacingError(
            f"Error: VM '{vm_name}' still exists after delete attempt.\n"
            f"Try again: clawbox delete {vm_number}"
        )

    marker_file.unlink(missing_ok=True)
    stop_vm_watcher(STATE_DIR, vm_name)
    _deactivate_mutagen_sync(vm_name, flush=False, reason="delete_vm")
    cleanup_locks_for_vm(vm_name)
    print(f"Deleted VM: {vm_name}")


def ip_vm(vm_number: int, tart: TartClient) -> None:
    vm_name = vm_name_for(vm_number)
    _require_vm_exists(tart, vm_name, vm_number)
    _require_vm_running(tart, vm_name, vm_number)
    ip = tart.ip(vm_name)
    if not ip:
        raise UserFacingError(
            f"Error: Could not resolve IP for '{vm_name}'.\n"
            "Wait for the VM to finish booting and retry."
        )
    print(ip)


def reconcile_runtime(tart: TartClient) -> None:
    reconcile_vm_watchers(tart, STATE_DIR)
    reconcile_vm_sync(tart, STATE_DIR)


def watch_vm(vm_name: str, state_dir: Path, poll_seconds: int, tart: TartClient) -> None:
    run_vm_watcher_loop(
        tart=tart,
        state_dir=state_dir,
        vm_name=vm_name,
        poll_seconds=poll_seconds,
    )


def status_vm(vm_number: int, tart: TartClient, *, as_json: bool = False) -> None:
    status_vm_impl(vm_number, tart, as_json=as_json, context=_status_context())


def status_environment(tart: TartClient, *, as_json: bool = False) -> None:
    status_environment_impl(tart, as_json=as_json, context=_status_context())
