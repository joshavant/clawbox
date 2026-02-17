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


def test_readme_uses_clawbox_safe_watch_recipe() -> None:
    readme = (PROJECT_DIR / "README.md").read_text(encoding="utf-8")
    guide = (PROJECT_DIR / "DEVELOPER.md").read_text(encoding="utf-8")
    assert "scripts/run-node.mjs gateway --force" in readme
    assert "--watch-path src --watch-path tsconfig.json --watch-path package.json" in readme
    assert "PATH=/opt/homebrew/bin:$PATH" not in readme
    assert "pnpm gateway:watch\n" not in readme
    assert "pnpm gateway:watch --force" not in readme
    assert "pnpm gateway:watch\n" in guide
    assert "pnpm gateway:watch --force" not in guide


def test_openclaw_role_links_developer_source_without_runtime_wrappers() -> None:
    role = (PROJECT_DIR / "ansible" / "roles" / "openclaw" / "tasks" / "main.yml").read_text(
        encoding="utf-8"
    )
    assert "Run fast-fail OpenClaw source build gate" in role
    assert "command: pnpm exec tsdown" in role
    assert "Link synced OpenClaw source as global openclaw command" in role
    assert "command: npm link" in role
    assert "openclaw-dev" not in role


def test_homebrew_role_sets_path_for_login_and_non_login_zsh_shells() -> None:
    role = (PROJECT_DIR / "ansible" / "roles" / "homebrew" / "tasks" / "main.yml").read_text(
        encoding="utf-8"
    )
    assert 'path: "/Users/{{ vm_name }}/.zprofile"' in role
    assert 'line: \'eval "$(/opt/homebrew/bin/brew shellenv)"\'' in role
    assert "Ensure Homebrew is on PATH for non-login zsh shells" in role
    assert "path: /etc/zshenv" in role
    assert "CLAWBOX HOMEBREW PATH" in role
    assert "/opt/homebrew/bin:/opt/homebrew/sbin:$PATH" in role


def test_homebrew_role_fails_fast_when_brew_binary_is_missing_after_install() -> None:
    role = (PROJECT_DIR / "ansible" / "roles" / "homebrew" / "tasks" / "main.yml").read_text(
        encoding="utf-8"
    )
    assert "Run Homebrew installer" in role
    assert "Verify Homebrew binary is present" in role
    assert "Fail when Homebrew install did not produce brew binary" in role
    assert "Expected binary is missing: /opt/homebrew/bin/brew" in role


def test_provision_playbook_runs_network_preflight_before_homebrew() -> None:
    playbook = (PROJECT_DIR / "ansible" / "playbooks" / "provision.yml").read_text(encoding="utf-8")
    network_idx = playbook.index("- role: network_preflight")
    homebrew_idx = playbook.index("- role: homebrew")
    assert network_idx < homebrew_idx


def test_network_preflight_has_no_guest_dns_mutation_step() -> None:
    role = (
        PROJECT_DIR / "ansible" / "roles" / "network_preflight" / "tasks" / "main.yml"
    ).read_text(encoding="utf-8")
    assert "CLAWBOX_TEST_FORCE_NETWORK_PREFLIGHT_FAIL" in role
    assert "networksetup -setdnsservers" not in role


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
