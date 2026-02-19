from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


def _load_integration_module():
    module_path = Path(__file__).resolve().parents[1] / "integration_py" / "run_integration.py"
    spec = importlib.util.spec_from_file_location("clawbox_integration_runner", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load integration runner module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _integration_config(
    module,
    *,
    standard_vm_number: int = 91,
    developer_vm_number: int = 92,
    optional_vm_number: int = 93,
):
    return module.IntegrationConfig(
        profile="full",
        standard_vm_number=standard_vm_number,
        developer_vm_number=developer_vm_number,
        optional_vm_number=optional_vm_number,
        base_image_name="macos-base",
        base_image_remote="ghcr.io/cirruslabs/macos-sequoia-vanilla:latest",
        exhaustive=False,
        keep_failed_artifacts=False,
        allow_destructive_cleanup=False,
        ansible_connect_timeout=8,
        ansible_command_timeout=30,
        remote_shell_timeout_seconds=120,
    )


def _fake_completed_process(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")


def test_cleanup_vm_removes_marker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = _load_integration_module()
    monkeypatch.setattr(module, "vm_base_name", lambda: "clawbox")
    runner = module.IntegrationRunner(tmp_path, _integration_config(module, developer_vm_number=92))
    marker = runner.marker_path(runner.developer_vm_name)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("profile: developer\n", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_run_cmd(args, **_kwargs):
        calls.append(args)
        return _fake_completed_process(args)

    monkeypatch.setattr(runner, "run_cmd", fake_run_cmd)

    runner.cleanup_vm(runner.developer_vm_name)

    assert calls == [
        ["tart", "stop", runner.developer_vm_name],
        ["tart", "delete", runner.developer_vm_name],
    ]
    assert not marker.exists()


def test_cleanup_all_removes_all_markers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = _load_integration_module()
    monkeypatch.setattr(module, "vm_base_name", lambda: "clawbox")
    runner = module.IntegrationRunner(
        tmp_path,
        _integration_config(module, standard_vm_number=91, developer_vm_number=92, optional_vm_number=93),
    )

    vm_names = {runner.standard_vm_name, runner.developer_vm_name, runner.optional_vm_name}
    markers = []
    for vm_name in vm_names:
        marker = runner.marker_path(vm_name)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("profile: standard\n", encoding="utf-8")
        markers.append(marker)

    calls: list[list[str]] = []

    def fake_run_cmd(args, **_kwargs):
        calls.append(args)
        return _fake_completed_process(args)

    monkeypatch.setattr(runner, "run_cmd", fake_run_cmd)

    runner.cleanup_all()

    assert len(calls) == len(vm_names) * 2
    assert all(not marker.exists() for marker in markers)
    assert not runner.tmp_root.exists()


def test_keep_failed_artifacts_preserves_state_on_unexpected_exception(monkeypatch: pytest.MonkeyPatch):
    module = _load_integration_module()
    cleanup_calls: list[int] = []

    class FakeRunner:
        def __init__(self, project_dir, config):
            self.project_dir = project_dir
            self.config = config

        def run(self):
            raise RuntimeError("unexpected failure")

        def cleanup_all(self):
            cleanup_calls.append(1)

    config = module.IntegrationConfig(
        profile="full",
        standard_vm_number=91,
        developer_vm_number=92,
        optional_vm_number=92,
        base_image_name="macos-base",
        base_image_remote="ghcr.io/cirruslabs/macos-sequoia-vanilla:latest",
        exhaustive=False,
        keep_failed_artifacts=True,
        allow_destructive_cleanup=False,
        ansible_connect_timeout=8,
        ansible_command_timeout=30,
        remote_shell_timeout_seconds=120,
    )

    monkeypatch.setattr(module, "load_config", lambda: config)
    monkeypatch.setattr(module, "IntegrationRunner", FakeRunner)

    with pytest.raises(RuntimeError, match="unexpected failure"):
        module.main()

    assert cleanup_calls == []


def test_unexpected_exception_cleans_up_when_keep_disabled(monkeypatch: pytest.MonkeyPatch):
    module = _load_integration_module()
    cleanup_calls: list[int] = []

    class FakeRunner:
        def __init__(self, project_dir, config):
            self.project_dir = project_dir
            self.config = config

        def run(self):
            raise RuntimeError("unexpected failure")

        def cleanup_all(self):
            cleanup_calls.append(1)

    config = module.IntegrationConfig(
        profile="full",
        standard_vm_number=91,
        developer_vm_number=92,
        optional_vm_number=92,
        base_image_name="macos-base",
        base_image_remote="ghcr.io/cirruslabs/macos-sequoia-vanilla:latest",
        exhaustive=False,
        keep_failed_artifacts=False,
        allow_destructive_cleanup=False,
        ansible_connect_timeout=8,
        ansible_command_timeout=30,
        remote_shell_timeout_seconds=120,
    )

    monkeypatch.setattr(module, "load_config", lambda: config)
    monkeypatch.setattr(module, "IntegrationRunner", FakeRunner)

    with pytest.raises(RuntimeError, match="unexpected failure"):
        module.main()

    assert cleanup_calls == [1]


def test_cleanup_guard_skips_cleanup_when_target_vms_exist(monkeypatch: pytest.MonkeyPatch):
    module = _load_integration_module()
    cleanup_calls: list[int] = []

    class FakeRunner:
        def __init__(self, project_dir, config):
            self.project_dir = project_dir
            self.config = config
            self.cleanup_safe = False

        def run(self):
            raise module.IntegrationError("target VM(s) already exist")

        def cleanup_all(self):
            cleanup_calls.append(1)

    config = module.IntegrationConfig(
        profile="full",
        standard_vm_number=91,
        developer_vm_number=92,
        optional_vm_number=92,
        base_image_name="macos-base",
        base_image_remote="ghcr.io/cirruslabs/macos-sequoia-vanilla:latest",
        exhaustive=False,
        keep_failed_artifacts=False,
        allow_destructive_cleanup=False,
        ansible_connect_timeout=8,
        ansible_command_timeout=30,
        remote_shell_timeout_seconds=120,
    )

    monkeypatch.setattr(module, "load_config", lambda: config)
    monkeypatch.setattr(module, "IntegrationRunner", FakeRunner)

    with pytest.raises(SystemExit):
        module.main()

    assert cleanup_calls == []


def test_cleanup_failure_does_not_mask_primary_failure(monkeypatch: pytest.MonkeyPatch, capsys):
    module = _load_integration_module()

    class FakeRunner:
        def __init__(self, project_dir, config):
            self.project_dir = project_dir
            self.config = config
            self.cleanup_safe = True

        def run(self):
            raise RuntimeError("primary failure")

        def cleanup_all(self):
            raise RuntimeError("cleanup failure")

    config = module.IntegrationConfig(
        profile="full",
        standard_vm_number=91,
        developer_vm_number=92,
        optional_vm_number=92,
        base_image_name="macos-base",
        base_image_remote="ghcr.io/cirruslabs/macos-sequoia-vanilla:latest",
        exhaustive=False,
        keep_failed_artifacts=False,
        allow_destructive_cleanup=False,
        ansible_connect_timeout=8,
        ansible_command_timeout=30,
        remote_shell_timeout_seconds=120,
    )

    monkeypatch.setattr(module, "load_config", lambda: config)
    monkeypatch.setattr(module, "IntegrationRunner", FakeRunner)

    with pytest.raises(RuntimeError, match="primary failure"):
        module.main()

    captured = capsys.readouterr()
    assert "Cleanup failed after an earlier integration failure" in captured.err


def test_cleanup_failure_surfaces_when_run_succeeds(monkeypatch: pytest.MonkeyPatch):
    module = _load_integration_module()

    class FakeRunner:
        def __init__(self, project_dir, config):
            self.project_dir = project_dir
            self.config = config
            self.cleanup_safe = True

        def run(self):
            return None

        def cleanup_all(self):
            raise RuntimeError("cleanup failure")

    config = module.IntegrationConfig(
        profile="full",
        standard_vm_number=91,
        developer_vm_number=92,
        optional_vm_number=92,
        base_image_name="macos-base",
        base_image_remote="ghcr.io/cirruslabs/macos-sequoia-vanilla:latest",
        exhaustive=False,
        keep_failed_artifacts=False,
        allow_destructive_cleanup=False,
        ansible_connect_timeout=8,
        ansible_command_timeout=30,
        remote_shell_timeout_seconds=120,
    )

    monkeypatch.setattr(module, "load_config", lambda: config)
    monkeypatch.setattr(module, "IntegrationRunner", FakeRunner)

    with pytest.raises(RuntimeError, match="cleanup failure"):
        module.main()


def test_load_config_defaults_profile_to_full(monkeypatch: pytest.MonkeyPatch):
    module = _load_integration_module()
    monkeypatch.delenv("CLAWBOX_CI_PROFILE", raising=False)
    config = module.load_config()
    assert config.profile == "full"


def test_load_config_rejects_invalid_profile(monkeypatch: pytest.MonkeyPatch):
    module = _load_integration_module()
    monkeypatch.setenv("CLAWBOX_CI_PROFILE", "invalid")
    with pytest.raises(module.IntegrationError, match="CLAWBOX_CI_PROFILE must be one of"):
        module.load_config()


def test_load_config_accepts_mutagen_contract_profile(monkeypatch: pytest.MonkeyPatch):
    module = _load_integration_module()
    monkeypatch.setenv("CLAWBOX_CI_PROFILE", "mutagen-contract")
    config = module.load_config()
    assert config.profile == "mutagen-contract"


def test_standard_network_preflight_failure_flow_sets_fault_injection_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    module = _load_integration_module()
    monkeypatch.setattr(module, "vm_base_name", lambda: "clawbox")
    runner = module.IntegrationRunner(
        tmp_path,
        _integration_config(module, standard_vm_number=91, developer_vm_number=92, optional_vm_number=93),
    )

    cleanup_calls: list[str] = []
    provision_envs: list[dict[str, str] | None] = []

    def fake_cleanup_vm(vm_name: str) -> None:
        cleanup_calls.append(vm_name)

    def fake_run_cmd(args, **kwargs):
        if args[:4] == ["python3", "-m", "clawbox", "provision"]:
            provision_envs.append(kwargs.get("env"))
            return subprocess.CompletedProcess(
                args=args,
                returncode=1,
                stdout="VM networking preflight failed before Homebrew install.\n",
                stderr="",
            )
        return _fake_completed_process(args)

    monkeypatch.setattr(runner, "cleanup_vm", fake_cleanup_vm)
    monkeypatch.setattr(runner, "run_cmd", fake_run_cmd)

    runner.run_standard_network_preflight_failure_flow()

    assert cleanup_calls == [runner.standard_vm_name]
    assert provision_envs
    assert provision_envs[0] is not None
    assert provision_envs[0]["CLAWBOX_TEST_FORCE_NETWORK_PREFLIGHT_FAIL"] == "1"
