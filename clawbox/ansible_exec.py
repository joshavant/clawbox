from __future__ import annotations

import os
import subprocess
from pathlib import Path

from clawbox.errors import UserFacingError


def build_ansible_shell_command(
    *,
    inventory_path: str,
    vm_name: str,
    shell_cmd: str,
    ansible_user: str,
    ansible_password: str,
    connect_timeout_seconds: int,
    command_timeout_seconds: int,
    become: bool = False,
) -> list[str]:
    cmd = [
        "ansible",
        "-i",
        inventory_path,
        vm_name,
        "-T",
        str(connect_timeout_seconds),
        "-m",
        "shell",
        "-a",
        shell_cmd,
        "-e",
        f"ansible_user={ansible_user}",
        "-e",
        f"ansible_password={ansible_password}",
        "-e",
        f"ansible_command_timeout={command_timeout_seconds}",
        "-e",
        "ansible_become=false",
    ]
    if become:
        cmd.append("-b")
        cmd += ["-e", "ansible_become=true", "-e", f"ansible_become_password={ansible_password}"]
    return cmd


def build_ansible_env() -> dict[str, str]:
    env = os.environ.copy()
    env["ANSIBLE_HOST_KEY_CHECKING"] = "False"
    return env


def run_ansible_shell(
    *,
    ansible_dir: Path,
    inventory_path: str,
    vm_name: str,
    shell_cmd: str,
    ansible_user: str,
    ansible_password: str,
    connect_timeout_seconds: int,
    command_timeout_seconds: int,
    become: bool = False,
) -> subprocess.CompletedProcess[str]:
    cmd = build_ansible_shell_command(
        inventory_path=inventory_path,
        vm_name=vm_name,
        shell_cmd=shell_cmd,
        ansible_user=ansible_user,
        ansible_password=ansible_password,
        connect_timeout_seconds=connect_timeout_seconds,
        command_timeout_seconds=command_timeout_seconds,
        become=become,
    )
    try:
        return subprocess.run(
            cmd,
            cwd=ansible_dir,
            check=False,
            text=True,
            capture_output=True,
            env=build_ansible_env(),
        )
    except FileNotFoundError as exc:
        raise UserFacingError("Error: Command not found: ansible") from exc
