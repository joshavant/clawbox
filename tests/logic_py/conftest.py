from __future__ import annotations

from pathlib import Path

import pytest

from clawbox import orchestrator


@pytest.fixture(autouse=True)
def isolate_orchestrator_runtime_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home_dir = tmp_path / "home"
    ansible_dir = tmp_path / "ansible"
    state_dir = tmp_path / ".clawbox" / "state"
    secrets_file = ansible_dir / "secrets.yml"

    home_dir.mkdir(parents=True, exist_ok=True)
    ansible_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setattr(orchestrator, "ANSIBLE_DIR", ansible_dir)
    monkeypatch.setattr(orchestrator, "SECRETS_FILE", secrets_file)
    monkeypatch.setattr(orchestrator, "STATE_DIR", state_dir)
