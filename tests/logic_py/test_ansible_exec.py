from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from clawbox import ansible_exec
from clawbox.errors import UserFacingError


def test_build_ansible_shell_command_defaults() -> None:
    cmd = ansible_exec.build_ansible_shell_command(
        inventory_path="192.168.64.10,",
        vm_name="clawbox-91",
        shell_cmd="true",
        ansible_user="clawbox-91",
        ansible_password="secret",
        connect_timeout_seconds=8,
        command_timeout_seconds=30,
        become=False,
    )
    assert cmd[:6] == ["ansible", "-i", "192.168.64.10,", "clawbox-91", "-T", "8"]
    ssh_args = (
        "-o IdentitiesOnly=yes "
        "-o PubkeyAuthentication=no "
        "-o PreferredAuthentications=password,keyboard-interactive "
        "-o NumberOfPasswordPrompts=1"
    )
    assert f"--ssh-common-args={ssh_args}" in cmd
    assert "ansible_become=false" in cmd
    assert "-b" not in cmd
    assert "ansible_become=true" not in cmd


def test_build_ansible_shell_command_become_true() -> None:
    cmd = ansible_exec.build_ansible_shell_command(
        inventory_path="192.168.64.11,",
        vm_name="clawbox-92",
        shell_cmd="id",
        ansible_user="clawbox-92",
        ansible_password="pw",
        connect_timeout_seconds=10,
        command_timeout_seconds=60,
        become=True,
    )
    assert "-b" in cmd
    assert "ansible_become=true" in cmd
    assert "ansible_become_password=pw" in cmd


def test_build_ansible_env_disables_host_key_checking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXAMPLE_ENV", "ok")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    env = ansible_exec.build_ansible_env()
    assert env["EXAMPLE_ENV"] == "ok"
    assert env["ANSIBLE_HOST_KEY_CHECKING"] == "False"
    assert env["PYTHONPATH"] == str(Path(ansible_exec.__file__).resolve().parents[1])


def test_build_ansible_env_prepends_package_root_to_existing_pythonpath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/tmp/existing")

    env = ansible_exec.build_ansible_env()

    package_root = str(Path(ansible_exec.__file__).resolve().parents[1])
    assert env["PYTHONPATH"] == f"{package_root}{os.pathsep}/tmp/existing"


def test_build_ansible_env_does_not_duplicate_existing_package_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = str(Path(ansible_exec.__file__).resolve().parents[1])
    monkeypatch.setenv("PYTHONPATH", f"{package_root}{os.pathsep}/tmp/existing")

    env = ansible_exec.build_ansible_env()

    assert env["PYTHONPATH"] == f"{package_root}{os.pathsep}/tmp/existing"


def test_run_ansible_shell_runs_with_expected_params(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            args=["ansible"],
            returncode=0,
            stdout="ok",
            stderr="",
        )

    monkeypatch.setattr(ansible_exec.subprocess, "run", fake_run)
    proc = ansible_exec.run_ansible_shell(
        ansible_dir=tmp_path,
        inventory_path="inventory/tart_inventory.py",
        vm_name="clawbox-91",
        shell_cmd="echo hi",
        ansible_user="admin",
        ansible_password="admin",
        connect_timeout_seconds=8,
        command_timeout_seconds=30,
        become=False,
    )
    assert proc.returncode == 0
    kwargs = seen["kwargs"]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["check"] is False
    assert kwargs["text"] is True
    assert kwargs["capture_output"] is True
    assert kwargs["env"]["ANSIBLE_HOST_KEY_CHECKING"] == "False"
    assert str(Path(ansible_exec.__file__).resolve().parents[1]) in kwargs["env"]["PYTHONPATH"].split(
        os.pathsep
    )


def test_run_ansible_shell_maps_missing_ansible(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def raise_not_found(*args, **kwargs):
        raise FileNotFoundError("ansible not found")

    monkeypatch.setattr(ansible_exec.subprocess, "run", raise_not_found)
    with pytest.raises(UserFacingError, match="Command not found: ansible"):
        ansible_exec.run_ansible_shell(
            ansible_dir=tmp_path,
            inventory_path="inventory/tart_inventory.py",
            vm_name="clawbox-91",
            shell_cmd="true",
            ansible_user="admin",
            ansible_password="admin",
            connect_timeout_seconds=8,
            command_timeout_seconds=30,
            become=False,
        )
