from __future__ import annotations

from pathlib import Path

from clawbox import config


def _set_group_vars(monkeypatch, path: Path) -> None:
    monkeypatch.setattr(config, "GROUP_VARS_ALL", path)
    config._group_vars_all_text.cache_clear()
    config.vm_base_name.cache_clear()


def test_vm_base_name_reads_group_vars(monkeypatch, tmp_path: Path) -> None:
    group_vars = tmp_path / "all.yml"
    group_vars.write_text('vm_base_name: "clawtest"\n', encoding="utf-8")
    _set_group_vars(monkeypatch, group_vars)

    assert config.vm_base_name() == "clawtest"
    assert config.vm_name_for(92) == "clawtest-92"


def test_vm_base_name_falls_back_to_default_on_invalid_value(monkeypatch, tmp_path: Path) -> None:
    group_vars = tmp_path / "all.yml"
    group_vars.write_text('vm_base_name: "bad/name"\n', encoding="utf-8")
    _set_group_vars(monkeypatch, group_vars)

    assert config.vm_base_name() == config.DEFAULT_VM_BASE_NAME
