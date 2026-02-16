from __future__ import annotations

from pathlib import Path
from typing import Callable

from clawbox.secrets import read_vm_password


def vm_user_credentials(
    vm_name: str,
    *,
    secrets_file: Path,
    read_password: Callable[[Path], str] = read_vm_password,
) -> tuple[str, str]:
    return vm_name, read_password(secrets_file)
