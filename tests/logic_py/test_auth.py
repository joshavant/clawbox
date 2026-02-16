from __future__ import annotations

from pathlib import Path

import pytest

from clawbox.auth import vm_user_credentials


def test_vm_user_credentials_reads_from_secrets_file(tmp_path: Path) -> None:
    secrets_file = tmp_path / "secrets.yml"
    secrets_file.write_text('vm_password: "clawbox"\n', encoding="utf-8")
    user, password = vm_user_credentials(
        "clawbox-1",
        secrets_file=secrets_file,
    )
    assert user == "clawbox-1"
    assert password == "clawbox"


def test_vm_user_credentials_raises_when_secrets_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        vm_user_credentials(
            "clawbox-1",
            secrets_file=tmp_path / "missing-secrets.yml",
        )


def test_vm_user_credentials_surfaces_read_errors(tmp_path: Path) -> None:
    secrets_file = tmp_path / "secrets.yml"
    secrets_file.write_text("ignored\n", encoding="utf-8")
    with pytest.raises(OSError, match="boom"):
        vm_user_credentials(
            "clawbox-1",
            secrets_file=secrets_file,
            read_password=lambda _path: (_ for _ in ()).throw(OSError("boom")),
        )
