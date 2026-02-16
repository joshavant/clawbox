from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from clawbox.ansible_exec import build_ansible_env, build_ansible_shell_command
from clawbox.config import group_var_scalar, vm_base_name
from clawbox.locks import (
    OPENCLAW_PAYLOAD_LOCK,
    OPENCLAW_SOURCE_LOCK,
    SIGNAL_PAYLOAD_LOCK,
    locked_path_for_vm,
)
from clawbox.secrets import ensure_vm_password_file, read_vm_password
from clawbox.tart import TartClient


class IntegrationError(RuntimeError):
    """Raised when an integration assertion fails."""


SIGNAL_PAYLOAD_MOUNT = group_var_scalar(
    "signal_cli_payload_mount", "/Users/Shared/clawbox-sync/signal-cli-payload"
)
SIGNAL_PAYLOAD_MARKER_FILENAME = group_var_scalar(
    "signal_cli_payload_marker_filename", ".clawbox-signal-payload-host-marker"
)


@dataclass
class IntegrationConfig:
    profile: str
    standard_vm_number: int
    developer_vm_number: int
    optional_vm_number: int
    base_image_name: str
    base_image_remote: str
    exhaustive: bool
    keep_failed_artifacts: bool
    allow_destructive_cleanup: bool
    ansible_connect_timeout: int
    ansible_command_timeout: int
    remote_shell_timeout_seconds: int


class IntegrationRunner:
    def __init__(self, project_dir: Path, config: IntegrationConfig):
        self.project_dir = project_dir
        self.config = config
        self.tart = TartClient()
        self.vm_base_name = vm_base_name()

        self.standard_vm_name = f"{self.vm_base_name}-{self.config.standard_vm_number}"
        self.developer_vm_name = f"{self.vm_base_name}-{self.config.developer_vm_number}"
        self.optional_vm_name = f"{self.vm_base_name}-{self.config.optional_vm_number}"

        self.secrets_file = self.project_dir / "ansible" / "secrets.yml"
        self.tmp_root = Path(tempfile.mkdtemp())
        self.fixture_source_dir = self.tmp_root / "openclaw-source"
        self.fixture_payload_dir = self.tmp_root / "openclaw-payload"
        self.fixture_signal_payload_dir = self.tmp_root / "signal-cli-payload"
        self.ready_marker_dir = self.tmp_root / "ready"
        self.ready_marker_dir.mkdir(parents=True, exist_ok=True)
        self.vm_password = ""
        self.cleanup_safe = True

    def fail(self, msg: str) -> None:
        raise IntegrationError(msg)

    def require_cmd(self, cmd: str) -> None:
        if shutil.which(cmd):
            return
        self.fail(f"Error: Required command not found: {cmd}")

    def run_cmd(
        self,
        args: list[str],
        *,
        check: bool = True,
        capture_output: bool = False,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            check=check,
            text=True,
            capture_output=capture_output,
            env=env,
            cwd=cwd or self.project_dir,
        )

    def wait_for_vm_ip(self, vm_name: str, timeout_seconds: int = 120) -> str:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            ip = self.tart.ip(vm_name)
            if ip:
                return ip
            time.sleep(2)
        self.fail(f"Assertion failed: '{vm_name}' did not report an IP address within {timeout_seconds}s")
        return ""

    def clawbox_cmd(self, *args: str) -> list[str]:
        return ["python3", "-m", "clawbox", *args]

    def ensure_prerequisites(self) -> None:
        for cmd in ("tart", "ansible", "ansible-playbook", "mutagen"):
            self.require_cmd(cmd)
        if os.uname().sysname != "Darwin":
            self.fail("Error: Integration tests require macOS (Darwin).")

    def cleanup_vm(self, vm_name: str) -> None:
        self.run_cmd(["tart", "stop", vm_name], check=False, capture_output=True)
        self.run_cmd(["tart", "delete", vm_name], check=False, capture_output=True)
        self.marker_path(vm_name).unlink(missing_ok=True)

    def cleanup_all(self) -> None:
        for vm_name in {self.standard_vm_name, self.developer_vm_name, self.optional_vm_name}:
            self.cleanup_vm(vm_name)
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    def ensure_safe_cleanup_targets(self) -> None:
        if self.config.allow_destructive_cleanup:
            return
        existing = [
            vm_name
            for vm_name in {self.standard_vm_name, self.developer_vm_name, self.optional_vm_name}
            if self.tart.vm_exists(vm_name)
        ]
        if not existing:
            return
        self.cleanup_safe = False
        self.fail(
            "Error: Integration target VM(s) already exist and would be deleted by pre-cleanup.\n"
            f"  targets: {', '.join(sorted(existing))}\n"
            "Set CLAWBOX_CI_ALLOW_DESTRUCTIVE_CLEANUP=true to allow deletion, "
            "or use different CLAWBOX_CI_*_VM_NUMBER values."
        )

    def create_secrets_if_missing(self) -> None:
        ensure_vm_password_file(self.secrets_file, create_if_missing=True)

    def load_vm_password(self) -> None:
        try:
            self.vm_password = read_vm_password(self.secrets_file)
        except (OSError, ValueError) as exc:
            self.fail(str(exc))

    def ensure_base_image(self) -> None:
        proc = self.run_cmd(["tart", "list", "--quiet"], capture_output=True)
        images = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
        if self.config.base_image_name in images:
            return
        print(
            f"==> Cloning base image '{self.config.base_image_name}' from '{self.config.base_image_remote}'"
        )
        self.run_cmd(["tart", "clone", self.config.base_image_remote, self.config.base_image_name])

    def marker_path(self, vm_name: str) -> Path:
        return self.project_dir / ".clawbox" / "state" / f"{vm_name}.provisioned"

    def watcher_record_path(self, vm_name: str) -> Path:
        return self.project_dir / ".clawbox" / "state" / "watchers" / f"{vm_name}.json"

    def assert_eventually(
        self,
        condition: Callable[[], bool],
        *,
        timeout_seconds: int,
        poll_seconds: int,
        failure_message: str,
    ) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if condition():
                return
            time.sleep(poll_seconds)
        self.fail(failure_message)

    def assert_file_contains(self, file: Path, needle: str) -> None:
        if not file.exists():
            self.fail(f"Assertion failed: expected file does not exist: {file}")
        haystack = file.read_text(encoding="utf-8")
        if needle in haystack:
            return
        self.fail(
            f"Assertion failed: '{needle}' not found in {file}\n"
            "----- file contents -----\n"
            f"{haystack}"
            "-------------------------"
        )

    def assert_host_file_eventually_contains(
        self, path: Path, expected_substring: str, timeout_seconds: int = 30
    ) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if path.exists():
                try:
                    data = path.read_text(encoding="utf-8")
                except OSError:
                    data = ""
                if expected_substring in data:
                    return
            time.sleep(2)
        self.fail(
            "Assertion failed: host file did not receive expected content within timeout\n"
            f"  file: {path}\n"
            f"  expected substring: {expected_substring!r}"
        )

    def assert_vm_running(self, vm_name: str) -> None:
        if self.tart.vm_running(vm_name):
            return
        vm_list = self.run_cmd(["tart", "list", "--format", "json"], check=False, capture_output=True)
        self.fail(
            f"Assertion failed: VM '{vm_name}' is not reported as running\n"
            f"{vm_list.stdout}"
        )

    def assert_vm_absent(self, vm_name: str) -> None:
        if not self.tart.vm_exists(vm_name):
            return
        vm_list = self.run_cmd(["tart", "list", "--format", "json"], check=False, capture_output=True)
        self.fail(
            f"Assertion failed: VM '{vm_name}' is still present\n"
            f"{vm_list.stdout}"
        )

    def run_ansible_shell(self, vm_name: str, shell_cmd: str) -> bool:
        cmd = build_ansible_shell_command(
            inventory_path="ansible/inventory/tart_inventory.py",
            vm_name=vm_name,
            shell_cmd=shell_cmd,
            ansible_user=vm_name,
            ansible_password=self.vm_password,
            connect_timeout_seconds=self.config.ansible_connect_timeout,
            command_timeout_seconds=self.config.ansible_command_timeout,
            become=False,
        )
        proc = subprocess.run(
            cmd,
            check=False,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=self.project_dir,
            env=build_ansible_env(),
        )
        return proc.returncode == 0

    def wait_for_remote_shell(self, vm_name: str, timeout_seconds: int | None = None) -> None:
        timeout = timeout_seconds if timeout_seconds is not None else self.config.remote_shell_timeout_seconds
        marker_file = self.ready_marker_dir / f"{vm_name}.ready"
        if marker_file.exists():
            return

        print(f"  waiting for SSH readiness on {vm_name} (timeout: {timeout}s)...")
        poll_seconds = 5
        elapsed = 0
        while elapsed < timeout:
            if self.run_ansible_shell(vm_name, "true"):
                marker_file.parent.mkdir(parents=True, exist_ok=True)
                marker_file.touch()
                print(f"  SSH ready: {vm_name}")
                return
            time.sleep(poll_seconds)
            elapsed += poll_seconds

        self.fail(
            f"Assertion failed: '{vm_name}' did not become reachable over SSH/Ansible within {timeout}s"
        )

    def assert_remote_test(self, vm_name: str, shell_test: str) -> None:
        self.wait_for_remote_shell(vm_name)
        if self.run_ansible_shell(vm_name, shell_test):
            return
        self.fail(f"Assertion failed on '{vm_name}': {shell_test}")

    def assert_remote_command(self, vm_name: str, *args: str) -> None:
        self.wait_for_remote_shell(vm_name)
        cmd = "export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH; " + shlex.join(list(args))
        if self.run_ansible_shell(vm_name, cmd):
            return
        self.fail(f"Assertion failed on '{vm_name}': command '{' '.join(args)}'")

    def create_openclaw_fixture(self) -> None:
        (self.fixture_source_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (self.fixture_source_dir / "tools" / "tsdown-mock").mkdir(parents=True, exist_ok=True)
        self.fixture_payload_dir.mkdir(parents=True, exist_ok=True)
        self.fixture_signal_payload_dir.mkdir(parents=True, exist_ok=True)
        (self.fixture_signal_payload_dir / "host-fixture-note.txt").write_text(
            "host fixture payload\n",
            encoding="utf-8",
        )

        (self.fixture_source_dir / "package.json").write_text(
            textwrap.dedent(
                """\
                {
                  "name": "openclaw-ci-fixture",
                  "version": "0.0.0",
                  "private": true,
                  "bin": {
                    "openclaw": "scripts/openclaw.js"
                  },
                  "devDependencies": {
                    "tsdown": "file:tools/tsdown-mock"
                  },
                  "scripts": {
                    "gateway:watch": "node scripts/gateway-watch.js"
                  }
                }
                """
            ),
            encoding="utf-8",
        )
        (self.fixture_source_dir / "pnpm-lock.yaml").write_text(
            textwrap.dedent(
                """\
                lockfileVersion: '9.0'

                settings:
                  autoInstallPeers: true
                  excludeLinksFromLockfile: false

                importers:
                  .:
                    devDependencies:
                      tsdown:
                        specifier: file:tools/tsdown-mock
                        version: file:tools/tsdown-mock

                packages:

                  tsdown@file:tools/tsdown-mock:
                    resolution: {directory: tools/tsdown-mock, type: directory}
                    hasBin: true

                snapshots:

                  tsdown@file:tools/tsdown-mock: {}
                """
            ),
            encoding="utf-8",
        )
        (self.fixture_source_dir / "tools" / "tsdown-mock" / "package.json").write_text(
            textwrap.dedent(
                """\
                {
                  "name": "tsdown",
                  "version": "0.0.0",
                  "bin": {
                    "tsdown": "index.js"
                  }
                }
                """
            ),
            encoding="utf-8",
        )
        tsdown_script = self.fixture_source_dir / "tools" / "tsdown-mock" / "index.js"
        tsdown_script.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env node

                const fs = require('node:fs')
                const path = require('node:path')

                const marker = path.join(process.cwd(), '.clawbox-tsdown-gate-ok')
                fs.writeFileSync(marker, 'ok\\n', 'utf8')
                console.log('tsdown fixture gate ok')
                process.exit(0)
                """
            ),
            encoding="utf-8",
        )
        os.chmod(tsdown_script, 0o755)
        openclaw_script = self.fixture_source_dir / "scripts" / "openclaw.js"
        openclaw_script.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env node

                const args = process.argv.slice(2)
                if (args.includes('--version')) {
                  console.log('openclaw-ci-fixture-0.0.0')
                  process.exit(0)
                }
                console.log('openclaw fixture invoked:', args.join(' '))
                """
            ),
            encoding="utf-8",
        )
        os.chmod(openclaw_script, 0o755)
        gateway_watch_script = self.fixture_source_dir / "scripts" / "gateway-watch.js"
        gateway_watch_script.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env node

                const args = process.argv.slice(2)
                if (args.includes('--help')) {
                  console.log('openclaw gateway:watch fixture help')
                  process.exit(0)
                }
                console.log('openclaw gateway:watch fixture invoked:', args.join(' '))
                process.exit(0)
                """
            ),
            encoding="utf-8",
        )
        os.chmod(gateway_watch_script, 0o755)

    def seed_pnpm_like_symlink_chain(self) -> None:
        pnpm_modules = self.fixture_source_dir / "node_modules" / ".pnpm" / "node_modules"
        pnpm_modules.mkdir(parents=True, exist_ok=True)
        packages_clawdbot = self.fixture_source_dir / "packages" / "clawdbot"
        packages_clawdbot.mkdir(parents=True, exist_ok=True)

        package_nm_link = packages_clawdbot / "node_modules"
        if package_nm_link.exists() or package_nm_link.is_symlink():
            package_nm_link.unlink()
        # This mirrors pnpm-style relative links that can recurse deeply when symlinks are followed.
        os.symlink("../../../node_modules/.pnpm/node_modules", package_nm_link)

        clawdbot_link = pnpm_modules / "clawdbot"
        if clawdbot_link.exists() or clawdbot_link.is_symlink():
            clawdbot_link.unlink()
        os.symlink("../../../packages/clawdbot", clawdbot_link)

    def create_openclaw_invalid_gate_fixture(self) -> tuple[Path, Path]:
        invalid_source_dir = self.tmp_root / "openclaw-source-invalid"
        invalid_payload_dir = self.tmp_root / "openclaw-payload-invalid"
        shutil.rmtree(invalid_source_dir, ignore_errors=True)
        shutil.rmtree(invalid_payload_dir, ignore_errors=True)
        shutil.copytree(self.fixture_source_dir, invalid_source_dir, dirs_exist_ok=True)
        invalid_payload_dir.mkdir(parents=True, exist_ok=True)

        failing_tsdown_script = invalid_source_dir / "tools" / "tsdown-mock" / "index.js"
        failing_tsdown_script.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env node

                console.error('tsdown fixture gate failure')
                process.exit(23)
                """
            ),
            encoding="utf-8",
        )
        os.chmod(failing_tsdown_script, 0o755)
        return invalid_source_dir, invalid_payload_dir

    def run_developer_invalid_source_gate_flow(self) -> None:
        print(f"==> integration: developer invalid-source gate flow ({self.developer_vm_name})")
        self.cleanup_vm(self.developer_vm_name)
        marker = self.marker_path(self.developer_vm_name)
        marker.unlink(missing_ok=True)
        invalid_source_dir, invalid_payload_dir = self.create_openclaw_invalid_gate_fixture()

        up_failed = self.run_cmd(
            self.clawbox_cmd(
                "up",
                "--developer",
                "--number",
                str(self.config.developer_vm_number),
                "--openclaw-source",
                str(invalid_source_dir),
                "--openclaw-payload",
                str(invalid_payload_dir),
            ),
            check=False,
            capture_output=True,
        )
        up_failed_output = f"{up_failed.stdout}\n{up_failed.stderr}"
        if up_failed.returncode == 0:
            self.fail(
                "Assertion failed: expected developer up to fail for invalid source tsdown gate\n"
                f"----- output -----\n{up_failed_output}"
            )
        if "Run fast-fail OpenClaw source build gate" not in up_failed_output:
            self.fail(
                "Assertion failed: expected failure output to include fast gate task label\n"
                f"----- output -----\n{up_failed_output}"
            )
        if "tsdown fixture gate failure" not in up_failed_output:
            self.fail(
                "Assertion failed: expected failure output to include tsdown error details\n"
                f"----- output -----\n{up_failed_output}"
            )
        if "Provisioning failed." not in up_failed_output:
            self.fail(
                "Assertion failed: expected failure output to include provisioning failure summary\n"
                f"----- output -----\n{up_failed_output}"
            )
        if marker.exists():
            self.fail(
                "Assertion failed: expected no provision marker after tsdown-gate provisioning failure"
            )

    def run_developer_signal_payload_marker_guard_flow(self) -> None:
        print(f"==> integration: developer signal payload marker guard ({self.developer_vm_name})")
        vm_name = self.developer_vm_name
        marker = self.marker_path(vm_name)
        host_marker = self.fixture_signal_payload_dir / SIGNAL_PAYLOAD_MARKER_FILENAME
        guest_marker = f"{SIGNAL_PAYLOAD_MOUNT}/{SIGNAL_PAYLOAD_MARKER_FILENAME}"

        self.cleanup_vm(vm_name)
        marker.unlink(missing_ok=True)
        try:
            self.run_cmd(self.clawbox_cmd("create", str(self.config.developer_vm_number)))
            self.run_cmd(
                self.clawbox_cmd(
                    "launch",
                    "--developer",
                    str(self.config.developer_vm_number),
                    "--openclaw-source",
                    str(self.fixture_source_dir),
                    "--openclaw-payload",
                    str(self.fixture_payload_dir),
                    "--signal-cli-payload",
                    str(self.fixture_signal_payload_dir),
                    "--headless",
                )
            )
            vm_ip = self.wait_for_vm_ip(vm_name, timeout_seconds=120)

            host_marker.unlink(missing_ok=True)
            wipe_guest_marker_cmd = build_ansible_shell_command(
                inventory_path=f"{vm_ip},",
                vm_name=vm_ip,
                shell_cmd=f"rm -f {shlex.quote(guest_marker)}",
                ansible_user="admin",
                ansible_password="admin",
                connect_timeout_seconds=self.config.ansible_connect_timeout,
                command_timeout_seconds=self.config.ansible_command_timeout,
                become=False,
            )
            self.run_cmd(
                wipe_guest_marker_cmd,
                check=False,
                capture_output=True,
                env=build_ansible_env(),
                cwd=self.project_dir / "ansible",
            )

            provision_failed = self.run_cmd(
                self.clawbox_cmd(
                    "provision",
                    str(self.config.developer_vm_number),
                    "--developer",
                    "--add-signal-cli-provisioning",
                    "--enable-signal-payload",
                ),
                check=False,
                capture_output=True,
            )
            output = f"{provision_failed.stdout}\n{provision_failed.stderr}"
            if provision_failed.returncode == 0:
                self.fail(
                    "Assertion failed: expected developer provision to fail when signal payload marker is missing\n"
                    f"----- output -----\n{output}"
                )
            if "signal-cli payload marker was not visible in the guest" not in output:
                self.fail(
                    "Assertion failed: expected signal payload marker guard failure message\n"
                    f"----- output -----\n{output}"
                )
            if guest_marker not in output:
                self.fail(
                    "Assertion failed: expected missing marker path in provision failure output\n"
                    f"----- output -----\n{output}"
                )
        finally:
            self.cleanup_vm(vm_name)
            marker.unlink(missing_ok=True)

    def run_standard_flow(self) -> None:
        print(f"==> integration: standard flow ({self.standard_vm_name})")
        self.cleanup_vm(self.standard_vm_name)
        self.marker_path(self.standard_vm_name).unlink(missing_ok=True)

        self.run_cmd(self.clawbox_cmd("create", str(self.config.standard_vm_number)))
        self.run_cmd(
            self.clawbox_cmd(
                "launch",
                str(self.config.standard_vm_number),
                "--headless",
            ),
        )
        self.run_cmd(self.clawbox_cmd("provision", str(self.config.standard_vm_number)))

        self.assert_vm_running(self.standard_vm_name)
        self.assert_file_contains(self.marker_path(self.standard_vm_name), "profile: standard")

        up_output = self.run_cmd(
            self.clawbox_cmd("up", str(self.config.standard_vm_number)),
            capture_output=True,
        ).stdout
        if up_output:
            print(up_output, end="")
        if "Provision marker found" not in up_output:
            self.fail("Assertion failed: expected 'clawbox up' to skip provisioning when marker exists")

        print(f"  verifying post-provisioning checks on {self.standard_vm_name}...")
        self.assert_vm_running(self.standard_vm_name)
        self.assert_remote_command(self.standard_vm_name, "openclaw", "--version")

    def run_vm_management_flow(self) -> None:
        print(f"==> integration: vm management commands ({self.standard_vm_name})")
        ip_output = (
            self.run_cmd(
                self.clawbox_cmd("ip", str(self.config.standard_vm_number)),
                capture_output=True,
            )
            .stdout.strip()
        )
        if not ip_output or "." not in ip_output:
            self.fail(f"Assertion failed: expected IPv4 output from 'clawbox ip', got: {ip_output!r}")

        self.run_cmd(self.clawbox_cmd("down", str(self.config.standard_vm_number)))
        if self.tart.vm_running(self.standard_vm_name):
            self.fail(f"Assertion failed: expected '{self.standard_vm_name}' to be stopped")

        provision_start = time.monotonic()
        provision_when_stopped = self.run_cmd(
            self.clawbox_cmd("provision", str(self.config.standard_vm_number)),
            check=False,
            capture_output=True,
        )
        provision_elapsed = time.monotonic() - provision_start
        provision_output = f"{provision_when_stopped.stdout}\n{provision_when_stopped.stderr}"
        if provision_when_stopped.returncode == 0:
            self.fail("Assertion failed: expected 'clawbox provision' to fail when VM is stopped")
        if "is not running" not in provision_output:
            self.fail(
                "Assertion failed: expected stopped-VM provision error to mention 'is not running'\n"
                f"----- output -----\n{provision_output}"
            )
        if "waiting for VM IP" in provision_output:
            self.fail(
                "Assertion failed: stopped-VM provision unexpectedly reached IP wait stage\n"
                f"----- output -----\n{provision_output}"
            )
        if provision_elapsed > 15:
            self.fail(
                "Assertion failed: stopped-VM provision did not fail fast\n"
                f"  elapsed_seconds={provision_elapsed:.2f}"
            )

        ip_when_stopped = self.run_cmd(
            self.clawbox_cmd("ip", str(self.config.standard_vm_number)),
            check=False,
            capture_output=True,
        )
        if ip_when_stopped.returncode == 0:
            self.fail("Assertion failed: expected 'clawbox ip' to fail when VM is stopped")

        self.run_cmd(
            self.clawbox_cmd(
                "launch",
                str(self.config.standard_vm_number),
                "--headless",
            )
        )
        self.assert_vm_running(self.standard_vm_name)

        self.run_cmd(self.clawbox_cmd("delete", str(self.config.standard_vm_number)))
        self.assert_vm_absent(self.standard_vm_name)
        self.marker_path(self.standard_vm_name).unlink(missing_ok=True)

    def run_developer_flow(self) -> None:
        print(f"==> integration: developer flow ({self.developer_vm_name})")
        self.cleanup_vm(self.developer_vm_name)
        self.marker_path(self.developer_vm_name).unlink(missing_ok=True)
        self.seed_pnpm_like_symlink_chain()

        up_result = self.run_cmd(
            self.clawbox_cmd(
                "up",
                "--developer",
                "--number",
                str(self.config.developer_vm_number),
                "--openclaw-source",
                str(self.fixture_source_dir),
                "--openclaw-payload",
                str(self.fixture_payload_dir),
                "--add-signal-cli-provisioning",
                "--signal-cli-payload",
                str(self.fixture_signal_payload_dir),
            ),
            check=False,
            capture_output=True,
        )
        up_output = f"{up_result.stdout}\n{up_result.stderr}"
        if up_output:
            print(up_output, end="")
        if up_result.returncode != 0:
            self.fail(
                "Assertion failed: expected developer up to succeed\n"
                f"----- output -----\n{up_output}"
            )
        relaunch_marker = "Provisioning completed; relaunching"
        if relaunch_marker not in up_output:
            self.fail(
                "Assertion failed: expected relaunch message in developer up output\n"
                f"----- output -----\n{up_output}"
            )
        pre_relaunch_output, post_relaunch_output = up_output.split(relaunch_marker, 1)
        if pre_relaunch_output.count("Preparing Mutagen sync...") != 1:
            self.fail(
                "Assertion failed: expected exactly one Mutagen sync preparation block "
                "before provisioning completion/relaunch in developer up flow\n"
                f"----- output -----\n{up_output}"
            )
        if post_relaunch_output.count("Preparing Mutagen sync...") != 1:
            self.fail(
                "Assertion failed: expected exactly one Mutagen sync preparation block "
                "after GUI relaunch in developer up flow\n"
                f"----- output -----\n{up_output}"
            )
        if "signal-payload:" not in pre_relaunch_output:
            self.fail(
                "Assertion failed: expected signal payload sync path in pre-provision Mutagen block\n"
                f"----- output -----\n{up_output}"
            )
        if "signal-payload:" not in post_relaunch_output:
            self.fail(
                "Assertion failed: expected signal payload sync path in post-relaunch Mutagen block\n"
                f"----- output -----\n{up_output}"
            )
        if "Ensure synced developer paths are owned by VM user" not in up_output:
            self.fail(
                "Assertion failed: expected developer ownership reconciliation task output\n"
                f"----- output -----\n{up_output}"
            )
        if "File name too long" in up_output:
            self.fail(
                "Assertion failed: symlink-heavy source should not trigger filename-too-long ownership failure\n"
                f"----- output -----\n{up_output}"
            )
        if "synced developer paths verified." not in up_output:
            self.fail("Assertion failed: expected developer sync preflight verification output")

        status_output = self.run_cmd(
            self.clawbox_cmd("status", str(self.config.developer_vm_number)),
            capture_output=True,
        ).stdout
        if status_output:
            print(status_output, end="")
        if "sync paths:" not in status_output:
            self.fail("Assertion failed: expected sync paths section in developer status output")
        if "signal-cli-payload" not in status_output:
            self.fail("Assertion failed: expected signal payload sync path in developer status output")

        status_json_output = self.run_cmd(
            self.clawbox_cmd("status", str(self.config.developer_vm_number), "--json"),
            capture_output=True,
        ).stdout
        status_data = json.loads(status_json_output)
        if "sync_paths" not in status_data:
            self.fail("Assertion failed: expected sync_paths object in developer status --json")
        if status_data.get("signal_payload_sync", {}).get("enabled") is not True:
            self.fail("Assertion failed: expected signal_payload_sync.enabled=true in status --json")

        print(f"  verifying post-provisioning checks on {self.developer_vm_name}...")
        self.assert_vm_running(self.developer_vm_name)
        self.assert_file_contains(self.marker_path(self.developer_vm_name), "profile: developer")
        self.assert_file_contains(self.marker_path(self.developer_vm_name), "sync_backend: mutagen")
        self.assert_file_contains(self.marker_path(self.developer_vm_name), "signal_cli: true")
        self.assert_file_contains(self.marker_path(self.developer_vm_name), "signal_payload: true")
        self.assert_remote_test(
            self.developer_vm_name,
            f"test -L '/Users/{self.developer_vm_name}/Developer/openclaw'",
        )
        self.assert_remote_test(
            self.developer_vm_name,
            f"test -L '/Users/{self.developer_vm_name}/.openclaw'",
        )
        self.assert_remote_command(self.developer_vm_name, "openclaw", "--help")
        self.assert_remote_test(
            self.developer_vm_name,
            (
                f"test \"$(realpath /opt/homebrew/lib/node_modules/openclaw-ci-fixture)\" = "
                f"\"$(realpath /Users/{self.developer_vm_name}/Developer/openclaw)\""
            ),
        )
        self.assert_remote_test(
            self.developer_vm_name,
            f"test -f '/Users/{self.developer_vm_name}/Developer/openclaw/.clawbox-tsdown-gate-ok'",
        )
        self.assert_remote_command(self.developer_vm_name, "signal-cli", "--version")
        self.assert_remote_test(
            self.developer_vm_name,
            f"test -L '/Users/{self.developer_vm_name}/.local/share/signal-cli'",
        )
        self.assert_remote_test(
            self.developer_vm_name,
            (
                f"test \"$(realpath /Users/{self.developer_vm_name}/.local/share/signal-cli)\" = "
                f"\"{SIGNAL_PAYLOAD_MOUNT}\""
            ),
        )
        self.assert_remote_test(
            self.developer_vm_name,
            (
                f"test -f '/Users/{self.developer_vm_name}/.local/share/signal-cli/"
                "host-fixture-note.txt'"
            ),
        )
        self.assert_remote_command(
            self.developer_vm_name,
            "/bin/sh",
            "-lc",
            (
                "printf '%s\\n' 'guest-daemon-roundtrip-ok' > "
                f"'/Users/{self.developer_vm_name}/.local/share/signal-cli/guest-daemon-note.txt'"
            ),
        )
        self.assert_host_file_eventually_contains(
            self.fixture_signal_payload_dir / "guest-daemon-note.txt",
            "guest-daemon-roundtrip-ok",
            timeout_seconds=120,
        )
        self.assert_remote_command(
            self.developer_vm_name,
            "pnpm",
            "--dir",
            f"/Users/{self.developer_vm_name}/Developer/openclaw",
            "gateway:watch",
            "--help",
        )

    def run_optional_feature_flow(self) -> None:
        print(f"==> integration: optional feature flow ({self.optional_vm_name})")
        # Local macOS virtualization commonly caps concurrent VMs at 2.
        self.cleanup_vm(self.standard_vm_name)
        self.cleanup_vm(self.developer_vm_name)
        self.cleanup_vm(self.optional_vm_name)
        self.marker_path(self.optional_vm_name).unlink(missing_ok=True)

        up_output = self.run_cmd(
            self.clawbox_cmd(
                "up",
                "--number",
                str(self.config.optional_vm_number),
                "--add-playwright-provisioning",
                "--add-tailscale-provisioning",
                "--add-signal-cli-provisioning",
            ),
            capture_output=True,
        ).stdout
        if up_output:
            print(up_output, end="")

        status_output = self.run_cmd(
            self.clawbox_cmd("status", str(self.config.optional_vm_number)),
            capture_output=True,
        ).stdout
        if status_output:
            print(status_output, end="")

        status_json_output = self.run_cmd(
            self.clawbox_cmd("status", str(self.config.optional_vm_number), "--json"),
            capture_output=True,
        ).stdout
        status_data = json.loads(status_json_output)
        if status_data.get("signal_payload_sync", {}).get("enabled") is not False:
            self.fail("Assertion failed: expected signal_payload_sync.enabled=false in status --json")

        print(f"  verifying post-provisioning checks on {self.optional_vm_name}...")
        self.assert_vm_running(self.optional_vm_name)
        marker = self.marker_path(self.optional_vm_name)
        self.assert_file_contains(marker, "profile: standard")
        self.assert_file_contains(marker, "playwright: true")
        self.assert_file_contains(marker, "tailscale: true")
        self.assert_file_contains(marker, "signal_cli: true")
        self.assert_file_contains(marker, "signal_payload: false")

        self.assert_remote_command(self.optional_vm_name, "playwright", "--version")
        self.assert_remote_test(self.optional_vm_name, "test -d /Applications/Tailscale.app")
        self.assert_remote_test(
            self.optional_vm_name,
            f"test -L '/Users/{self.optional_vm_name}/Desktop/Tailscale.app'",
        )
        self.assert_remote_command(self.optional_vm_name, "signal-cli", "--version")

    def run_status_warning_flow(self) -> None:
        print("==> integration: status warning flow (invalid secrets)")
        vm_number = str(self.config.developer_vm_number)
        original = self.secrets_file.read_text(encoding="utf-8")
        try:
            self.secrets_file.write_text("not_vm_password: nope\n", encoding="utf-8")
            status_json_output = self.run_cmd(
                self.clawbox_cmd("status", vm_number, "--json"),
                capture_output=True,
            ).stdout
            status_data = json.loads(status_json_output)
            warnings = status_data.get("warnings")
            if not isinstance(warnings, list):
                self.fail("Assertion failed: expected warnings array in status --json")
            if not any("Could not parse vm_password" in warning for warning in warnings):
                self.fail(
                    "Assertion failed: expected vm_password parse warning in status --json warnings"
                )
        finally:
            self.secrets_file.write_text(original, encoding="utf-8")

    def run_developer_marker_migration_guard_flow(self) -> None:
        print(f"==> integration: developer marker migration guard ({self.developer_vm_name})")
        marker = self.marker_path(self.developer_vm_name)
        if not marker.exists():
            self.fail(f"Assertion failed: expected marker to exist before migration guard test: {marker}")

        original = marker.read_text(encoding="utf-8")
        legacy_lines = [
            line for line in original.splitlines() if not line.strip().startswith("sync_backend:")
        ]
        marker.write_text("\n".join(legacy_lines) + "\n", encoding="utf-8")
        try:
            up_failed = self.run_cmd(
                self.clawbox_cmd(
                    "up",
                    "--developer",
                    "--number",
                    str(self.config.developer_vm_number),
                    "--openclaw-source",
                    str(self.fixture_source_dir),
                    "--openclaw-payload",
                    str(self.fixture_payload_dir),
                    "--add-signal-cli-provisioning",
                    "--signal-cli-payload",
                    str(self.fixture_signal_payload_dir),
                ),
                check=False,
                capture_output=True,
            )
            output = f"{up_failed.stdout}\n{up_failed.stderr}"
            if up_failed.returncode == 0:
                self.fail(
                    "Assertion failed: expected developer up to fail for legacy marker migration guard\n"
                    f"----- output -----\n{output}"
                )
            if "legacy provision marker format" not in output:
                self.fail(
                    "Assertion failed: expected migration guard message for legacy marker format\n"
                    f"----- output -----\n{output}"
                )
            if "Recreate the VM instead" not in output:
                self.fail(
                    "Assertion failed: expected recreate guidance in migration guard output\n"
                    f"----- output -----\n{output}"
                )
        finally:
            marker.write_text(original, encoding="utf-8")

    def run_developer_out_of_band_shutdown_flow(self) -> None:
        print(f"==> integration: out-of-band shutdown watcher cleanup ({self.developer_vm_name})")
        vm_name = self.developer_vm_name
        watcher_record = self.watcher_record_path(vm_name)

        self.assert_vm_running(vm_name)
        if not watcher_record.exists():
            self.fail(
                "Assertion failed: expected watcher record file before out-of-band shutdown\n"
                f"  file: {watcher_record}"
            )
        if not locked_path_for_vm(OPENCLAW_SOURCE_LOCK, vm_name):
            self.fail("Assertion failed: expected source lock to exist before out-of-band shutdown")
        if not locked_path_for_vm(OPENCLAW_PAYLOAD_LOCK, vm_name):
            self.fail("Assertion failed: expected payload lock to exist before out-of-band shutdown")
        if not locked_path_for_vm(SIGNAL_PAYLOAD_LOCK, vm_name):
            self.fail("Assertion failed: expected signal payload lock to exist before out-of-band shutdown")

        self.run_cmd(["tart", "stop", vm_name], check=False, capture_output=True)
        self.assert_eventually(
            lambda: not self.tart.vm_running(vm_name),
            timeout_seconds=120,
            poll_seconds=2,
            failure_message=f"Assertion failed: expected VM to stop via out-of-band tart stop ({vm_name})",
        )
        self.assert_eventually(
            lambda: not watcher_record.exists(),
            timeout_seconds=45,
            poll_seconds=2,
            failure_message=(
                "Assertion failed: watcher record not cleaned after out-of-band VM shutdown\n"
                f"  file: {watcher_record}"
            ),
        )
        self.assert_eventually(
            lambda: locked_path_for_vm(OPENCLAW_SOURCE_LOCK, vm_name) == "",
            timeout_seconds=45,
            poll_seconds=2,
            failure_message="Assertion failed: source lock not released after out-of-band VM shutdown",
        )
        self.assert_eventually(
            lambda: locked_path_for_vm(OPENCLAW_PAYLOAD_LOCK, vm_name) == "",
            timeout_seconds=45,
            poll_seconds=2,
            failure_message="Assertion failed: payload lock not released after out-of-band VM shutdown",
        )
        self.assert_eventually(
            lambda: locked_path_for_vm(SIGNAL_PAYLOAD_LOCK, vm_name) == "",
            timeout_seconds=45,
            poll_seconds=2,
            failure_message="Assertion failed: signal payload lock not released after out-of-band VM shutdown",
        )

    def run(self) -> None:
        self.ensure_prerequisites()
        self.ensure_safe_cleanup_targets()
        if self.config.exhaustive and self.config.profile != "full":
            self.fail("Error: CLAWBOX_CI_EXHAUSTIVE=true requires CLAWBOX_CI_PROFILE=full")
        print("==> integration: pre-clean")
        self.cleanup_all()
        self.marker_path(self.standard_vm_name).unlink(missing_ok=True)
        self.marker_path(self.developer_vm_name).unlink(missing_ok=True)
        self.marker_path(self.optional_vm_name).unlink(missing_ok=True)

        self.create_secrets_if_missing()
        self.load_vm_password()
        self.ensure_base_image()
        print(f"==> integration: profile={self.config.profile} exhaustive={self.config.exhaustive}")

        self.run_standard_flow()
        self.run_vm_management_flow()

        if self.config.profile == "full":
            self.create_openclaw_fixture()
            self.run_developer_invalid_source_gate_flow()
            self.run_developer_signal_payload_marker_guard_flow()
            self.run_developer_flow()
            self.run_status_warning_flow()
            self.run_developer_marker_migration_guard_flow()
            self.run_developer_out_of_band_shutdown_flow()
        else:
            print(
                "==> integration: developer/status flows skipped "
                "(set CLAWBOX_CI_PROFILE=full to enable)"
            )

        if self.config.exhaustive:
            self.run_optional_feature_flow()
        else:
            print(
                "==> integration: optional feature flow skipped "
                "(set CLAWBOX_CI_EXHAUSTIVE=true to enable)"
            )

        print("==> integration: cleanup")
        print("Integration checks passed.")


def load_config() -> IntegrationConfig:
    def i(name: str, default: int) -> int:
        raw = os.getenv(name, str(default))
        try:
            return int(raw)
        except ValueError as exc:
            raise IntegrationError(f"Error: {name} must be an integer, got: {raw}") from exc

    def vm_i(name: str, default: int) -> int:
        value = i(name, default)
        if value < 1:
            raise IntegrationError(f"Error: {name} must be >= 1, got: {value}")
        return value

    exhaustive = os.getenv("CLAWBOX_CI_EXHAUSTIVE", "false").strip().lower() == "true"
    profile = os.getenv("CLAWBOX_CI_PROFILE", "full").strip().lower()
    if profile not in {"smoke", "full"}:
        raise IntegrationError(
            f"Error: CLAWBOX_CI_PROFILE must be one of: smoke, full (got: {profile})"
        )
    keep_failed_artifacts = (
        os.getenv("CLAWBOX_CI_KEEP_FAILURE_ARTIFACTS", "false").strip().lower() == "true"
    )
    allow_destructive_cleanup = (
        os.getenv("CLAWBOX_CI_ALLOW_DESTRUCTIVE_CLEANUP", "false").strip().lower() == "true"
    )
    return IntegrationConfig(
        profile=profile,
        standard_vm_number=vm_i("CLAWBOX_CI_STANDARD_VM_NUMBER", 91),
        developer_vm_number=vm_i("CLAWBOX_CI_DEVELOPER_VM_NUMBER", 92),
        optional_vm_number=vm_i("CLAWBOX_CI_OPTIONAL_VM_NUMBER", 92),
        base_image_name=os.getenv("CLAWBOX_CI_BASE_IMAGE", "macos-base"),
        base_image_remote=os.getenv(
            "CLAWBOX_CI_BASE_IMAGE_REMOTE",
            "ghcr.io/cirruslabs/macos-sequoia-vanilla:latest",
        ),
        exhaustive=exhaustive,
        keep_failed_artifacts=keep_failed_artifacts,
        allow_destructive_cleanup=allow_destructive_cleanup,
        ansible_connect_timeout=i("CLAWBOX_CI_ANSIBLE_CONNECT_TIMEOUT", 8),
        ansible_command_timeout=i("CLAWBOX_CI_ANSIBLE_COMMAND_TIMEOUT", 30),
        remote_shell_timeout_seconds=i("CLAWBOX_CI_REMOTE_SHELL_TIMEOUT_SECONDS", 120),
    )


def main() -> None:
    project_dir = Path(__file__).resolve().parents[2]
    config = load_config()
    runner = IntegrationRunner(project_dir, config)
    failed = False
    try:
        runner.run()
    except IntegrationError as exc:
        failed = True
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    except Exception:
        failed = True
        raise
    finally:
        if not getattr(runner, "cleanup_safe", True):
            print("Skipping integration cleanup due to safety guard.", file=sys.stderr)
        elif failed and config.keep_failed_artifacts:
            print(
                "Keeping VM/temp artifacts after failure "
                "(CLAWBOX_CI_KEEP_FAILURE_ARTIFACTS=true).",
                file=sys.stderr,
            )
        elif getattr(runner, "cleanup_all", None) is not None:
            try:
                runner.cleanup_all()
            except Exception as cleanup_exc:
                if failed:
                    print(
                        f"Warning: Cleanup failed after an earlier integration failure: {cleanup_exc}",
                        file=sys.stderr,
                    )
                else:
                    raise


if __name__ == "__main__":
    main()
