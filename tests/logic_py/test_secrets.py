from __future__ import annotations

from pathlib import Path

import pytest

from clawbox import secrets


def test_ensure_vm_password_file_creates_default(tmp_path: Path) -> None:
    path = tmp_path / "ansible" / "secrets.yml"
    created = secrets.ensure_vm_password_file(path, create_if_missing=True)
    assert created is True
    assert path.read_text(encoding="utf-8") == 'vm_password: "clawbox"\n'


def test_ensure_vm_password_file_missing_raises_when_creation_disabled(tmp_path: Path) -> None:
    path = tmp_path / "ansible" / "secrets.yml"
    with pytest.raises(FileNotFoundError):
        secrets.ensure_vm_password_file(path, create_if_missing=False)


def test_read_vm_password_parses_value(tmp_path: Path) -> None:
    path = tmp_path / "secrets.yml"
    path.write_text('vm_password: "secret"\n', encoding="utf-8")
    assert secrets.read_vm_password(path) == "secret"


def test_read_vm_password_rejects_invalid_content(tmp_path: Path) -> None:
    path = tmp_path / "secrets.yml"
    path.write_text("not_a_password: nope\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Could not parse vm_password"):
        secrets.read_vm_password(path)


def test_parse_vm_password_does_not_match_similar_key_names() -> None:
    text = '\n'.join(
        [
            'vm_password_old: "stale"',
            'vm_password: "fresh"',
        ]
    )
    assert secrets.parse_vm_password(text) == "fresh"


def test_parse_vm_password_preserves_hash_inside_quotes() -> None:
    text = 'vm_password: "abc#123"'
    assert secrets.parse_vm_password(text) == "abc#123"


def test_parse_vm_password_strips_hash_comment_outside_quotes() -> None:
    text = 'vm_password: abc123 # trailing comment'
    assert secrets.parse_vm_password(text) == "abc123"
