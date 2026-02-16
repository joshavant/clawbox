from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from clawbox.ansible_exec import run_ansible_shell


@dataclass(frozen=True)
class RemoteShellContext:
    ansible_dir: Path
    connect_timeout_seconds: int
    command_timeout_seconds: int
    default_inventory_path: str = "inventory/tart_inventory.py"


def ansible_shell(
    target: str,
    shell_cmd: str,
    *,
    ansible_user: str,
    ansible_password: str,
    become: bool,
    context: RemoteShellContext,
    inventory_path: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_ansible_shell(
        ansible_dir=context.ansible_dir,
        inventory_path=inventory_path or context.default_inventory_path,
        vm_name=target,
        shell_cmd=shell_cmd,
        ansible_user=ansible_user,
        ansible_password=ansible_password,
        connect_timeout_seconds=context.connect_timeout_seconds,
        command_timeout_seconds=context.command_timeout_seconds,
        become=become,
    )


def run_remote_path_probe(
    target: str,
    *,
    shell_cmd: str,
    paths: Sequence[str],
    ansible_user: str,
    ansible_password: str,
    parse_statuses: Callable[[str, Sequence[str]], dict[str, str]],
    context: RemoteShellContext | None = None,
    become: bool = False,
    inventory_path: str | None = None,
    shell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> tuple[int, dict[str, str], str]:
    if shell_runner is None:
        if context is None:
            raise ValueError("context is required when shell_runner is not provided")
        probe = ansible_shell(
            target,
            shell_cmd,
            ansible_user=ansible_user,
            ansible_password=ansible_password,
            become=become,
            context=context,
            inventory_path=inventory_path,
        )
    else:
        probe = shell_runner(
            target,
            shell_cmd,
            ansible_user=ansible_user,
            ansible_password=ansible_password,
            become=become,
            inventory_path=inventory_path or "inventory/tart_inventory.py",
        )
    statuses = parse_statuses(probe.stdout, paths)
    last_error = (probe.stderr or probe.stdout or "").strip()
    return probe.returncode, statuses, last_error


def wait_for_remote_probe(
    target: str,
    *,
    shell_cmd: str,
    paths: Sequence[str],
    ansible_user: str,
    ansible_password: str,
    parse_statuses: Callable[[str, Sequence[str]], dict[str, str]],
    is_success: Callable[[int, dict[str, str]], bool],
    timeout_seconds: int,
    context: RemoteShellContext | None = None,
    become: bool = False,
    inventory_path: str | None = None,
    poll_seconds: int = 2,
    shell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> tuple[bool, dict[str, str], str]:
    waited = 0
    last_statuses = {path: "unknown" for path in paths}
    last_error = ""
    while waited < timeout_seconds:
        returncode, statuses, probe_error = run_remote_path_probe(
            target,
            shell_cmd=shell_cmd,
            paths=paths,
            ansible_user=ansible_user,
            ansible_password=ansible_password,
            parse_statuses=parse_statuses,
            context=context,
            become=become,
            inventory_path=inventory_path,
            shell_runner=shell_runner,
        )
        last_statuses = statuses
        last_error = probe_error
        if is_success(returncode, statuses):
            return True, last_statuses, last_error
        time.sleep(poll_seconds)
        waited += poll_seconds

    return False, last_statuses, last_error
