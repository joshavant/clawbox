from __future__ import annotations

from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[2]


def test_developer_guide_uses_pep668_safe_install_flow() -> None:
    guide = (PROJECT_DIR / "DEVELOPER.md").read_text(encoding="utf-8")
    assert "pipx install --editable ." in guide
    assert "python3 -m pip install -e ." not in guide


def test_readme_and_developer_guide_define_orchestration_only_contract() -> None:
    readme = (PROJECT_DIR / "README.md").read_text(encoding="utf-8")
    guide = (PROJECT_DIR / "DEVELOPER.md").read_text(encoding="utf-8")
    assert "Clawbox is a tool for deploying OpenClaw-ready macOS VMs." in readme
    assert "OpenClaw itself stays unchanged." in readme
    assert "Clawbox is orchestration-only by design" in guide
    assert "clawbox recreate 1" in readme
    assert "clawbox recreate 1" in guide


def test_openclaw_role_links_developer_source_without_runtime_wrappers() -> None:
    role = (PROJECT_DIR / "ansible" / "roles" / "openclaw" / "tasks" / "main.yml").read_text(
        encoding="utf-8"
    )
    assert "Run fast-fail OpenClaw source build gate" in role
    assert "command: pnpm exec tsdown" in role
    assert "Link synced OpenClaw source as global openclaw command" in role
    assert "command: npm link" in role
    assert "openclaw-dev" not in role


def test_development_plan_references_python_cli_not_legacy_shell_wrappers() -> None:
    plan_file = PROJECT_DIR / "DEVELOPMENT_PLAN.md"
    if not plan_file.exists():
        return
    plan = plan_file.read_text(encoding="utf-8")
    assert "clawbox up" in plan
    assert "scripts/up.sh" not in plan
    assert "scripts/create-vm.sh" not in plan
    assert "scripts/launch-vm.sh" not in plan
    assert "scripts/provision-vm.sh" not in plan
    assert "safe-to-test" not in plan
