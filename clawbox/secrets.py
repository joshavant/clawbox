from __future__ import annotations

from pathlib import Path

from clawbox.scalar_parsing import parse_scalar

DEFAULT_VM_PASSWORD = "clawbox"


def secrets_file_contents(password: str = DEFAULT_VM_PASSWORD) -> str:
    return f'vm_password: "{password}"\n'


def missing_secrets_message(path: Path) -> str:
    return (
        f"Error: Secrets file not found: {path}\n\n"
        "Create it with:\n"
        f'  mkdir -p "{path.parent}"\n'
        f"  cat > \"{path}\" <<'EOF_SECRETS'\n"
        f"  {secrets_file_contents().rstrip()}\n"
        "  EOF_SECRETS\n"
        f'  chmod 600 "{path}"'
    )


def ensure_vm_password_file(path: Path, create_if_missing: bool) -> bool:
    if path.exists():
        return False
    if not create_if_missing:
        raise FileNotFoundError(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secrets_file_contents(), encoding="utf-8")
    path.chmod(0o600)
    return True


def parse_vm_password(text: str) -> str | None:
    value = parse_scalar(text, "vm_password")
    return value if value else None


def read_vm_password(path: Path) -> str:
    value = parse_vm_password(path.read_text(encoding="utf-8"))
    if not value:
        raise ValueError(f"Error: Could not parse vm_password from {path}")
    return value
