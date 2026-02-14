from __future__ import annotations

import subprocess
from pathlib import Path

from clawbox.errors import UserFacingError
from clawbox.paths import resolve_data_root

PROJECT_DIR = resolve_data_root()
PACKER_TEMPLATE = PROJECT_DIR / "packer" / "macos-base.pkr.hcl"


def _run_process(cmd: list[str], cwd: Path) -> None:
    try:
        proc = subprocess.run(cmd, cwd=cwd, check=False)
    except FileNotFoundError as exc:
        raise UserFacingError(f"Error: Command not found: {cmd[0]}") from exc
    if proc.returncode != 0:
        raise UserFacingError(
            f"Error: Command failed with exit code {proc.returncode}: {' '.join(cmd)}"
        )


def _packer_template_arg() -> str:
    try:
        return str(PACKER_TEMPLATE.relative_to(PROJECT_DIR))
    except ValueError:
        return str(PACKER_TEMPLATE)


def _ensure_packer_template() -> str:
    if not PACKER_TEMPLATE.exists():
        raise UserFacingError(f"Error: Packer template not found: {PACKER_TEMPLATE}")
    return _packer_template_arg()


def image_init() -> None:
    template_arg = _ensure_packer_template()
    print(f"Initializing packer plugins for template: {template_arg}")
    _run_process(["packer", "init", template_arg], cwd=PROJECT_DIR)


def image_build(skip_init: bool = False, force: bool = False) -> None:
    template_arg = _ensure_packer_template()
    if not skip_init:
        image_init()
    cmd = ["packer", "build"]
    if force:
        cmd.append("-force")
    cmd.append(template_arg)
    print(f"Building base image from template: {template_arg}")
    _run_process(cmd, cwd=PROJECT_DIR)
