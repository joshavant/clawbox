from __future__ import annotations

from pathlib import Path

from clawbox import paths


def _seed_data_root(root: Path) -> None:
    (root / "ansible" / "playbooks").mkdir(parents=True, exist_ok=True)
    (root / "ansible" / "playbooks" / "provision.yml").write_text("---\n", encoding="utf-8")
    (root / "packer").mkdir(parents=True, exist_ok=True)
    (root / "packer" / "macos-base.pkr.hcl").write_text("packer {}\n", encoding="utf-8")


def test_resolve_data_root_uses_env_override_when_valid(
    tmp_path: Path, monkeypatch
) -> None:
    env_root = tmp_path / "env-root"
    _seed_data_root(env_root)
    monkeypatch.setenv(paths.DATA_ROOT_ENV, str(env_root))
    monkeypatch.setattr(paths, "PACKAGE_ROOT", tmp_path / "package-root")
    monkeypatch.setattr(paths.sys, "prefix", str(tmp_path / "prefix"))

    assert paths.resolve_data_root() == env_root


def test_resolve_data_root_falls_back_to_prefix_share(tmp_path: Path, monkeypatch) -> None:
    prefix_root = tmp_path / "prefix" / "share" / "clawbox"
    _seed_data_root(prefix_root)
    monkeypatch.delenv(paths.DATA_ROOT_ENV, raising=False)
    monkeypatch.setattr(paths, "PACKAGE_ROOT", tmp_path / "package-root")
    monkeypatch.setattr(paths.sys, "prefix", str(tmp_path / "prefix"))

    assert paths.resolve_data_root() == prefix_root


def test_default_paths_use_repo_local_when_repo_mode(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    _seed_data_root(repo_root)
    monkeypatch.setattr(paths, "PACKAGE_ROOT", repo_root)
    monkeypatch.delenv(paths.STATE_DIR_ENV, raising=False)
    monkeypatch.delenv(paths.SECRETS_FILE_ENV, raising=False)

    assert paths.default_state_dir(repo_root) == repo_root / ".clawbox" / "state"
    assert paths.default_secrets_file(repo_root) == repo_root / "ansible" / "secrets.yml"


def test_default_paths_use_home_when_installed_mode(tmp_path: Path, monkeypatch) -> None:
    installed_root = tmp_path / "prefix" / "share" / "clawbox"
    _seed_data_root(installed_root)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(paths.STATE_DIR_ENV, raising=False)
    monkeypatch.delenv(paths.SECRETS_FILE_ENV, raising=False)

    assert paths.default_state_dir(installed_root) == (tmp_path / "home" / ".clawbox" / "state")
    assert paths.default_secrets_file(installed_root) == (tmp_path / "home" / ".clawbox" / "secrets.yml")
