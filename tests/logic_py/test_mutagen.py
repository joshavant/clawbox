from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clawbox import mutagen as mutagen_mod


def test_ensure_mutagen_ssh_alias_writes_include_and_host_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mutagen_mod.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        mutagen_mod, "_MUTAGEN_SSH_CONFIG_PATH", tmp_path / ".ssh" / "clawbox_mutagen_config"
    )
    alias = mutagen_mod.ensure_mutagen_ssh_alias(
        "clawbox-1",
        "192.168.64.201",
        "clawbox-1",
        tmp_path / "id_ed25519",
    )
    assert alias == "clawbox-mutagen-clawbox-1"

    main_config = tmp_path / ".ssh" / "config"
    managed_config = tmp_path / ".ssh" / "clawbox_mutagen_config"
    assert "Include ~/.ssh/clawbox_mutagen_config" in main_config.read_text(encoding="utf-8")
    managed = managed_config.read_text(encoding="utf-8")
    assert "Host clawbox-mutagen-clawbox-1" in managed
    assert "HostName 192.168.64.201" in managed


def test_remove_mutagen_ssh_alias_removes_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mutagen_mod.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        mutagen_mod, "_MUTAGEN_SSH_CONFIG_PATH", tmp_path / ".ssh" / "clawbox_mutagen_config"
    )
    mutagen_mod.ensure_mutagen_ssh_alias(
        "clawbox-1",
        "192.168.64.201",
        "clawbox-1",
        tmp_path / "id_ed25519",
    )
    mutagen_mod.remove_mutagen_ssh_alias("clawbox-1")
    managed = (tmp_path / ".ssh" / "clawbox_mutagen_config").read_text(encoding="utf-8")
    assert "CLAWBOX MUTAGEN BEGIN clawbox-1" not in managed


def test_ensure_vm_sessions_creates_bidirectional_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(mutagen_mod, "mutagen_available", lambda: True)
    monkeypatch.setattr(
        mutagen_mod,
        "_run_mutagen",
        lambda args, **_kwargs: calls.append(list(args)),
    )

    mutagen_mod.ensure_vm_sessions(
        "clawbox-1",
        "clawbox-mutagen-clawbox-1",
        [
            mutagen_mod.SessionSpec(
                kind="openclaw-source",
                host_path=Path("/tmp/source"),
                guest_path="/Users/clawbox-1/Developer/openclaw",
                ignore_vcs=True,
                ignored_paths=("node_modules",),
            ),
            mutagen_mod.SessionSpec(
                kind="openclaw-payload",
                host_path=Path("/tmp/payload"),
                guest_path="/Users/clawbox-1/.openclaw",
            ),
        ],
    )

    create_commands = [call for call in calls if call[:2] == ["sync", "create"]]
    assert len(create_commands) == 2
    assert all("--mode" in call and "two-way-resolved" in call for call in create_commands)
    assert any("--ignore-vcs" in call for call in create_commands)
    assert any("--ignore" in call and "node_modules" in call for call in create_commands)
    flush_commands = [call for call in calls if call[:2] == ["sync", "flush"]]
    assert flush_commands == [["sync", "flush", "--label-selector", "clawbox.vm=clawbox-1"]]


def test_reconcile_vm_sync_tears_down_inactive_vm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mutagen_mod.mark_vm_active(tmp_path, "clawbox-2")

    class _Tart:
        def vm_running(self, _vm_name: str) -> bool:
            return False

    torn_down: list[str] = []
    monkeypatch.setattr(
        mutagen_mod,
        "teardown_vm_sync",
        lambda _state_dir, vm_name, flush: torn_down.append(vm_name),
    )

    mutagen_mod.reconcile_vm_sync(_Tart(), tmp_path)
    assert torn_down == ["clawbox-2"]


def test_terminate_vm_sessions_is_noop_without_mutagen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mutagen_mod, "mutagen_available", lambda: False)
    monkeypatch.setattr(
        mutagen_mod,
        "_run_mutagen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not run mutagen")),
    )
    mutagen_mod.terminate_vm_sessions("clawbox-1", flush=True)


def test_vm_sessions_exist_uses_label_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mutagen_mod, "mutagen_available", lambda: True)

    seen: list[list[str]] = []

    def fake_run(args, **_kwargs):
        seen.append(list(args))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="sync_abc\n", stderr="")

    monkeypatch.setattr(mutagen_mod, "_run_mutagen", fake_run)

    assert mutagen_mod.vm_sessions_exist("clawbox-1") is True
    assert seen == [
        [
            "sync",
            "list",
            "--label-selector",
            "clawbox.vm=clawbox-1",
            "--template",
            '{{range .}}{{.Identifier}}{{"\\n"}}{{end}}',
        ]
    ]


def test_vm_sessions_status_prefers_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mutagen_mod, "mutagen_available", lambda: True)
    monkeypatch.setattr(
        mutagen_mod,
        "_run_mutagen",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args=args, returncode=0, stdout="session status", stderr="ignored"
        ),
    )

    assert mutagen_mod.vm_sessions_status("clawbox-1") == "session status"
