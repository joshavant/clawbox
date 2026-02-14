from __future__ import annotations

import os
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT_ENV = "CLAWBOX_DATA_DIR"
STATE_DIR_ENV = "CLAWBOX_STATE_DIR"
SECRETS_FILE_ENV = "CLAWBOX_SECRETS_FILE"


def _has_required_project_files(root: Path) -> bool:
    return (root / "ansible" / "playbooks" / "provision.yml").exists() and (
        root / "packer" / "macos-base.pkr.hcl"
    ).exists()


def resolve_data_root() -> Path:
    env_root = os.getenv(DATA_ROOT_ENV)
    if env_root:
        candidate = Path(env_root).expanduser()
        if _has_required_project_files(candidate):
            return candidate

    if _has_required_project_files(PACKAGE_ROOT):
        return PACKAGE_ROOT

    prefix_candidate = Path(sys.prefix) / "share" / "clawbox"
    if _has_required_project_files(prefix_candidate):
        return prefix_candidate

    return PACKAGE_ROOT


def _prefer_repo_local_paths(data_root: Path) -> bool:
    if data_root != PACKAGE_ROOT:
        return False
    if not _has_required_project_files(PACKAGE_ROOT):
        return False
    try:
        return os.access(PACKAGE_ROOT, os.W_OK)
    except OSError:
        return False


def default_state_dir(data_root: Path) -> Path:
    override = os.getenv(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    if _prefer_repo_local_paths(data_root):
        return data_root / ".clawbox" / "state"
    return Path.home() / ".clawbox" / "state"


def default_secrets_file(data_root: Path) -> Path:
    override = os.getenv(SECRETS_FILE_ENV)
    if override:
        return Path(override).expanduser()
    if _prefer_repo_local_paths(data_root):
        return data_root / "ansible" / "secrets.yml"
    return Path.home() / ".clawbox" / "secrets.yml"
