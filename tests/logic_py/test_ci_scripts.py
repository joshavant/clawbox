from __future__ import annotations

import os
import subprocess
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[2]


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=PROJECT_DIR,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def _write_stub(bin_dir: Path, name: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def test_ci_run_help() -> None:
    proc = _run(["bash", "scripts/ci/run.sh", "--help"])
    assert proc.returncode == 0
    assert "Usage: ./scripts/ci/run.sh <mode>" in proc.stdout


def test_ci_run_unknown_mode() -> None:
    proc = _run(["bash", "scripts/ci/run.sh", "not-a-mode"])
    output = f"{proc.stdout}\n{proc.stderr}"
    assert proc.returncode != 0
    assert "Unknown mode" in output


def test_ci_run_fast_with_stubbed_tooling(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for cmd in ("ansible-playbook", "shellcheck", "ansible-lint", "shfmt", "yamllint", "actionlint"):
        _write_stub(bin_dir, cmd)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    proc = _run(["bash", "scripts/ci/run.sh", "fast"], env=env)
    output = f"{proc.stdout}\n{proc.stderr}"
    assert proc.returncode == 0
    assert "Fast checks passed." in output


def test_pr_script_help() -> None:
    proc = _run(["./scripts/pr", "--help"])
    assert proc.returncode == 0
    assert "Local PR policy workflow helpers." in proc.stdout


def test_pr_script_title_validation() -> None:
    ok = _run(["./scripts/pr", "check-title", "--title", "feat: add release workflow"])
    assert ok.returncode == 0
    assert "PR title check passed." in ok.stdout

    bad = _run(["./scripts/pr", "check-title", "--title", "add release workflow"])
    output = f"{bad.stdout}\n{bad.stderr}"
    assert bad.returncode != 0
    assert "PR title must follow Conventional Commits" in output


def test_ci_bootstrap_help() -> None:
    proc = _run(["bash", "scripts/ci/bootstrap.sh", "--help"])
    assert proc.returncode == 0
    assert "Usage: ./scripts/ci/bootstrap.sh <mode>" in proc.stdout


def test_ci_bootstrap_unknown_mode() -> None:
    proc = _run(["bash", "scripts/ci/bootstrap.sh", "not-a-mode"])
    output = f"{proc.stdout}\n{proc.stderr}"
    assert proc.returncode != 0
    assert "Unknown mode" in output


def test_validate_script_with_stubbed_ansible_playbook(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    _write_stub(bin_dir, "ansible-playbook")

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    proc = _run(["bash", "scripts/validate.sh"], env=env)
    output = f"{proc.stdout}\n{proc.stderr}"
    assert proc.returncode == 0
    assert "Validation passed." in output
