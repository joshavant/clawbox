from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any


class TartError(RuntimeError):
    """Raised when a tart command fails in an orchestration-sensitive way."""


class TartClient:
    def _run(
        self,
        args: list[str],
        *,
        check: bool = True,
        capture_output: bool = True,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(
                args,
                check=False,
                text=True,
                capture_output=capture_output,
                cwd=cwd,
            )
        except FileNotFoundError as exc:
            cmd = args[0] if args else "command"
            raise TartError(f"Error: Command not found: {cmd}") from exc
        except OSError as exc:
            cmd = " ".join(args)
            raise TartError(f"Error: Could not run command '{cmd}': {exc}") from exc

        if check and proc.returncode != 0:
            cmd = " ".join(args)
            stderr = (proc.stderr or "").strip()
            stdout = (proc.stdout or "").strip()
            details = stderr or stdout
            if details:
                raise TartError(
                    f"Error: Command failed (exit {proc.returncode}): {cmd}\n{details}"
                )
            raise TartError(f"Error: Command failed (exit {proc.returncode}): {cmd}")
        return proc

    def list_vms_json(self) -> list[dict[str, Any]]:
        proc = self._run(["tart", "list", "--format", "json"])
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise TartError(f"Could not parse tart list output: {exc}") from exc
        if not isinstance(data, list):
            raise TartError("Unexpected tart list payload: expected a JSON list")
        return data

    def vm_exists(self, vm_name: str) -> bool:
        for vm in self.list_vms_json():
            if vm.get("Name") == vm_name:
                return True
        return False

    def vm_running(self, vm_name: str) -> bool:
        for vm in self.list_vms_json():
            if vm.get("Name") == vm_name:
                running = vm.get("Running")
                return bool(running) if isinstance(running, bool) else False
        return False

    def clone(self, base_image: str, vm_name: str) -> None:
        self._run(["tart", "clone", base_image, vm_name], check=True, capture_output=False)

    def stop(self, vm_name: str) -> None:
        self._run(["tart", "stop", vm_name], check=False)

    def delete(self, vm_name: str) -> None:
        self._run(["tart", "delete", vm_name], check=False)

    def ip(self, vm_name: str) -> str | None:
        for args in (
            ["tart", "ip", "--resolver=agent", vm_name],
            ["tart", "ip", vm_name],
        ):
            proc = self._run(args, check=False)
            ip = (proc.stdout or "").strip()
            if proc.returncode == 0 and ip:
                return ip
        return None

    def run_in_background(
        self, vm_name: str, run_args: list[str], log_file: Path
    ) -> subprocess.Popen[bytes]:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_file.open("wb")
        try:
            proc = subprocess.Popen(
                ["tart", "run", vm_name, *run_args],
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise TartError("Error: Command not found: tart") from exc
        except OSError as exc:
            raise TartError(f"Error: Could not start tart run for '{vm_name}': {exc}") from exc
        finally:
            log_handle.close()
        return proc


def wait_for_vm_running(
    tart: TartClient, vm_name: str, timeout_seconds: int, poll_seconds: int = 1
) -> bool:
    waited = 0
    while waited < timeout_seconds:
        if tart.vm_running(vm_name):
            return True
        time.sleep(poll_seconds)
        waited += poll_seconds
    return tart.vm_running(vm_name)
