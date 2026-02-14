from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from clawbox.ansible_exec import run_ansible_shell
from clawbox.config import group_var_scalar, vm_name_for
from clawbox.errors import UserFacingError, main_guard
from clawbox.image import image_build, image_init
from clawbox.locks import (
    OPENCLAW_PAYLOAD_LOCK,
    OPENCLAW_SOURCE_LOCK,
    SIGNAL_PAYLOAD_LOCK,
    LockError,
    acquire_path_lock,
    cleanup_locks_for_vm,
)
from clawbox.secrets import (
    ensure_vm_password_file,
    missing_secrets_message,
)
from clawbox.paths import default_secrets_file, default_state_dir, resolve_data_root
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


PROJECT_DIR = resolve_data_root()
ANSIBLE_DIR = PROJECT_DIR / "ansible"
SECRETS_FILE = default_secrets_file(PROJECT_DIR)
STATE_DIR = default_state_dir(PROJECT_DIR)
BASE_IMAGE = "macos-base"
DEFAULT_OPENCLAW_SOURCE_MOUNT = "/Volumes/My Shared Files/openclaw-source"
DEFAULT_OPENCLAW_PAYLOAD_MOUNT = "/Volumes/My Shared Files/openclaw-payload"
DEFAULT_SIGNAL_PAYLOAD_MOUNT = "/Volumes/My Shared Files/signal-cli-payload"
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
SIGNAL_SYNC_LABEL = group_var_scalar(
    "signal_cli_payload_sync_launchd_label", "com.clawbox.signal-cli-payload-sync"
)


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

def _status_context() -> StatusContext:
    return StatusContext(
        ansible_dir=ANSIBLE_DIR,
        state_dir=STATE_DIR,
        secrets_file=SECRETS_FILE,
        openclaw_source_mount=OPENCLAW_SOURCE_MOUNT,
        openclaw_payload_mount=OPENCLAW_PAYLOAD_MOUNT,
        signal_payload_mount=SIGNAL_PAYLOAD_MOUNT,
        signal_sync_label=SIGNAL_SYNC_LABEL,
        bootstrap_admin_user=BOOTSTRAP_ADMIN_USER,
        bootstrap_admin_password=BOOTSTRAP_ADMIN_PASSWORD,
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


def _tail_lines(path: Path, count: int = 20) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-count:])


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

    if tart.vm_running(vm_name):
        print(f"VM '{vm_name}' is already running.")
        return

    _validate_dirs([openclaw_source, openclaw_payload, signal_payload])
    _acquire_locks(tart, vm_name, openclaw_source, openclaw_payload, signal_payload)
    if signal_payload:
        _ensure_signal_payload_host_marker(signal_payload, vm_name)

    run_args: list[str] = []
    if profile == "developer":
        run_args.append(f"--dir=openclaw-source:{openclaw_source}")
        run_args.append(f"--dir=openclaw-payload:{openclaw_payload}")
    if signal_payload:
        run_args.append(f"--dir=signal-cli-payload:{signal_payload}")
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
        tail = _tail_lines(launch_log_file)
        if tail:
            msg.append(f"Recent tart output ({launch_log_file}):")
            msg.append(tail)
        raise UserFacingError(_with_virtualization_limit_hint("\n".join(msg)))

    if not wait_for_vm_running(tart, vm_name, timeout_seconds=30):
        msg = [
            f"Error: '{vm_name}' did not enter running state within 30s.",
            f"tart output log: {launch_log_file}",
        ]
        tail = _tail_lines(launch_log_file)
        if tail:
            msg.append(tail)
        raise UserFacingError(_with_virtualization_limit_hint("\n".join(msg)))

    print(f"VM started in background (PID {proc.pid}).")


@dataclass
class ProvisionOptions:
    vm_number: int
    profile: str
    enable_playwright: bool
    enable_tailscale: bool
    enable_signal_cli: bool
    enable_signal_payload: bool


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
    probe = _ansible_shell(
        ansible_target,
        shell_cmd,
        ansible_user=BOOTSTRAP_ADMIN_USER,
        ansible_password=BOOTSTRAP_ADMIN_PASSWORD,
        become=False,
        inventory_path=inventory_path,
    )
    statuses = _parse_mount_statuses(probe.stdout, paths)
    last_error = (probe.stderr or probe.stdout or "").strip()
    return probe.returncode, statuses, last_error


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
    waited = 0
    last_statuses = {path: "unknown" for path in paths}
    last_error = ""
    while waited < timeout_seconds:
        returncode, statuses, probe_error = _run_remote_path_probe(
            vm_name,
            shell_cmd=shell_cmd,
            paths=paths,
            inventory_path=inventory_path,
            target_host=target_host,
        )
        last_statuses = statuses
        last_error = probe_error
        if is_success(returncode, statuses):
            return True, last_statuses, last_error
        time.sleep(2)
        waited += 2

    return False, last_statuses, last_error


def _ansible_shell(
    vm_name: str,
    shell_cmd: str,
    *,
    ansible_user: str,
    ansible_password: str,
    become: bool = False,
    inventory_path: str = "inventory/tart_inventory.py",
) -> subprocess.CompletedProcess[str]:
    return run_ansible_shell(
        ansible_dir=ANSIBLE_DIR,
        inventory_path=inventory_path,
        vm_name=vm_name,
        shell_cmd=shell_cmd,
        ansible_user=ansible_user,
        ansible_password=ansible_password,
        connect_timeout_seconds=ANSIBLE_CONNECT_TIMEOUT_SECONDS,
        command_timeout_seconds=ANSIBLE_COMMAND_TIMEOUT_SECONDS,
        become=become,
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

    print("  verifying shared folder mounts...")
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
            print("  shared folder mounts verified.")
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
        "Error: Required shared folders failed preflight checks in the guest.",
        "Clawbox requires visible shared folder content before provisioning in developer mode.",
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

    cleanup_locks_for_vm(vm_name)


def delete_vm(vm_number: int, tart: TartClient) -> None:
    vm_name = vm_name_for(vm_number)
    marker_file = STATE_DIR / f"{vm_name}.provisioned"

    if not tart.vm_exists(vm_name):
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

def status_vm(vm_number: int, tart: TartClient, *, as_json: bool = False) -> None:
    status_vm_impl(vm_number, tart, as_json=as_json, context=_status_context())


def status_environment(tart: TartClient, *, as_json: bool = False) -> None:
    status_environment_impl(tart, as_json=as_json, context=_status_context())
