from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from clawbox.paths import resolve_data_root
from clawbox.scalar_parsing import parse_scalar

PROJECT_DIR = resolve_data_root()
GROUP_VARS_ALL = PROJECT_DIR / "ansible" / "group_vars" / "all.yml"
DEFAULT_VM_BASE_NAME = "clawbox"
_VM_BASE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")


@lru_cache(maxsize=1)
def _group_vars_all_text() -> str | None:
    if not GROUP_VARS_ALL.exists():
        return None

    try:
        return GROUP_VARS_ALL.read_text(encoding="utf-8")
    except OSError:
        return None


def group_var_scalar(key: str, default: str = "") -> str:
    text = _group_vars_all_text()
    if not text:
        return default

    value = parse_scalar(text, key)
    return value if value else default


@lru_cache(maxsize=1)
def vm_base_name() -> str:
    value = group_var_scalar("vm_base_name", DEFAULT_VM_BASE_NAME)
    if value and _VM_BASE_NAME_RE.match(value):
        return value
    return DEFAULT_VM_BASE_NAME


def vm_name_for(number: int) -> str:
    return f"{vm_base_name()}-{number}"
