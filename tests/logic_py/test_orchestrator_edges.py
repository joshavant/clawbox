from __future__ import annotations

import io
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from clawbox import orchestrator
from clawbox.locks import LockError
from clawbox.orchestrator import ProvisionOptions, UpOptions, UserFacingError
from clawbox.tart import TartError


class DummyProcess:
    def __init__(self, pid: int = 1000):
        self.pid = pid

    def poll(self):
        return None


class FakeTart:
    def __init__(self):
        self.exists: dict[str, bool] = {}
        self.running: dict[str, bool] = {}
        self.ip_map: dict[str, str | None] = {}
        self.stop_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.next_proc = DummyProcess()

    def vm_exists(self, vm_name: str) -> bool:
        return self.exists.get(vm_name, False)

    def vm_running(self, vm_name: str) -> bool:
        return self.running.get(vm_name, False)

    def clone(self, _base_image: str, vm_name: str) -> None:
        self.exists[vm_name] = True

    def run_in_background(self, _vm_name: str, _run_args: list[str], _log_file: Path):
        return self.next_proc

    def stop(self, vm_name: str) -> None:
        self.stop_calls.append(vm_name)
        self.running[vm_name] = False

    def delete(self, vm_name: str) -> None:
        self.delete_calls.append(vm_name)

    def ip(self, vm_name: str) -> str | None:
        return self.ip_map.get(vm_name)


@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(orchestrator, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(orchestrator, "ANSIBLE_DIR", tmp_path / "ansible")
    monkeypatch.setattr(orchestrator, "SECRETS_FILE", tmp_path / "ansible" / "secrets.yml")
    monkeypatch.setattr(orchestrator, "STATE_DIR", tmp_path / ".clawbox" / "state")
    monkeypatch.setattr(orchestrator, "start_vm_watcher", lambda *_args, **_kwargs: 9999)
    monkeypatch.setattr(orchestrator, "stop_vm_watcher", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(orchestrator, "mutagen_available", lambda: True)
    monkeypatch.setattr(orchestrator, "_activate_mutagen_sync", lambda **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_activate_mutagen_sync_from_locks", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_deactivate_mutagen_sync", lambda *_args, **_kwargs: None)
    (tmp_path / "ansible").mkdir(parents=True, exist_ok=True)
    yield tmp_path


def _capture_stdout(fn):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    return buf.getvalue()


def test_env_int_invalid_returns_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLAWBOX_TEST_INT", "not-an-int")
    assert orchestrator._env_int("CLAWBOX_TEST_INT", 42) == 42


def test_ensure_secrets_file_maps_missing_error(isolated_paths, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        orchestrator,
        "ensure_vm_password_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )
    with pytest.raises(UserFacingError, match="Secrets file not found"):
        orchestrator.ensure_secrets_file(create_if_missing=False)


def test_ensure_secrets_file_maps_oserror(isolated_paths, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        orchestrator,
        "ensure_vm_password_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("denied")),
    )
    with pytest.raises(UserFacingError, match="Could not write secrets file"):
        orchestrator.ensure_secrets_file(create_if_missing=True)


def test_tail_lines_reads_last_lines(isolated_paths):
    path = isolated_paths / "tail.log"
    path.write_text("1\n2\n3\n", encoding="utf-8")
    assert orchestrator.tail_lines(path, 2) == "2\n3"


def test_validate_profile_rejects_invalid():
    with pytest.raises(UserFacingError, match="--profile must be"):
        orchestrator._validate_profile("bad-profile")


def test_validate_dirs_rejects_missing(tmp_path: Path):
    with pytest.raises(UserFacingError, match="Expected directory does not exist"):
        orchestrator._validate_dirs([str(tmp_path / "missing")])


def test_signal_payload_host_marker_maps_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    marker_dir = tmp_path / "payload"
    marker_dir.mkdir(parents=True, exist_ok=True)

    def raise_oserror(*_args, **_kwargs):
        raise OSError("read only")

    monkeypatch.setattr(Path, "write_text", raise_oserror)
    with pytest.raises(UserFacingError, match="Could not write signal payload marker file"):
        orchestrator._ensure_signal_payload_host_marker(str(marker_dir), "clawbox-91")


def test_acquire_locks_maps_lock_error(monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    monkeypatch.setattr(
        orchestrator,
        "acquire_path_lock",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(LockError("lock held")),
    )
    with pytest.raises(UserFacingError, match="lock held"):
        orchestrator._acquire_locks(tart, "clawbox-91", "/src", "", "")


def test_launch_vm_maps_tart_launch_error(isolated_paths, monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    tart.exists["clawbox-91"] = True
    monkeypatch.setattr(orchestrator, "_acquire_locks", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        tart,
        "run_in_background",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TartError("run failed")),
    )
    with pytest.raises(UserFacingError, match="Failed to launch VM"):
        orchestrator.launch_vm(
            vm_number=91,
            profile="standard",
            openclaw_source="",
            openclaw_payload="",
            signal_payload="",
            headless=False,
            tart=tart,
        )


def test_launch_vm_running_vm_starts_watcher(isolated_paths, monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    vm_name = "clawbox-91"
    tart.exists[vm_name] = True
    tart.running[vm_name] = True
    watcher: dict[str, object] = {}

    monkeypatch.setattr(
        orchestrator,
        "start_vm_watcher",
        lambda state_dir, name: watcher.update({"state_dir": state_dir, "vm_name": name}) or 5150,
    )

    out = _capture_stdout(
        lambda: orchestrator.launch_vm(
            vm_number=91,
            profile="standard",
            openclaw_source="",
            openclaw_payload="",
            signal_payload="",
            headless=False,
            tart=tart,
        )
    )
    assert watcher["vm_name"] == vm_name
    assert "Watcher active (PID 5150)." in out


def test_launch_vm_developer_without_marker_uses_bootstrap_auth_mode(
    isolated_paths, monkeypatch: pytest.MonkeyPatch
):
    tart = FakeTart()
    vm_name = "clawbox-91"
    tart.exists[vm_name] = True
    tart.running[vm_name] = True
    seen_auth_modes: list[str] = []

    monkeypatch.setattr(orchestrator, "_acquire_locks", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "_activate_mutagen_sync",
        lambda **kwargs: seen_auth_modes.append(kwargs["auth_mode"]),
    )

    orchestrator.launch_vm(
        vm_number=91,
        profile="developer",
        openclaw_source=str(isolated_paths),
        openclaw_payload=str(isolated_paths),
        signal_payload="",
        headless=False,
        tart=tart,
    )
    assert seen_auth_modes == ["bootstrap_admin"]


def test_launch_vm_developer_with_marker_uses_vm_user_auth_mode(
    isolated_paths, monkeypatch: pytest.MonkeyPatch
):
    tart = FakeTart()
    vm_name = "clawbox-91"
    tart.exists[vm_name] = True
    tart.running[vm_name] = True
    seen_auth_modes: list[str] = []
    marker_file = orchestrator.STATE_DIR / f"{vm_name}.provisioned"
    marker_file.parent.mkdir(parents=True, exist_ok=True)
    marker_file.write_text("profile: developer\n", encoding="utf-8")

    monkeypatch.setattr(orchestrator, "_acquire_locks", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "_activate_mutagen_sync",
        lambda **kwargs: seen_auth_modes.append(kwargs["auth_mode"]),
    )

    orchestrator.launch_vm(
        vm_number=91,
        profile="developer",
        openclaw_source=str(isolated_paths),
        openclaw_payload=str(isolated_paths),
        signal_payload="",
        headless=False,
        tart=tart,
    )
    assert seen_auth_modes == ["vm_user"]


def test_resolve_vm_ip_returns_when_available():
    tart = FakeTart()
    tart.ip_map["clawbox-91"] = "192.168.64.55"
    assert orchestrator._resolve_vm_ip(tart, "clawbox-91", 1) == "192.168.64.55"


def test_resolve_mutagen_auth_uses_vm_user_only(monkeypatch: pytest.MonkeyPatch):
    vm_name = "clawbox-91"
    vm_ip = "192.168.64.10"
    attempted_users: list[str] = []

    monkeypatch.setattr(
        orchestrator,
        "vm_user_credentials",
        lambda _vm_name, *, secrets_file: (_vm_name, "dev-pass"),
    )

    def fake_ansible_shell(
        target: str,
        shell_cmd: str,
        *,
        ansible_user: str,
        ansible_password: str,
        become: bool = False,
        inventory_path: str = "inventory/tart_inventory.py",
    ) -> subprocess.CompletedProcess[str]:
        attempted_users.append(ansible_user)
        assert target == vm_ip
        assert shell_cmd == "true"
        assert become is False
        assert inventory_path == f"{vm_ip},"
        if ansible_user == vm_name and ansible_password == "dev-pass":
            return subprocess.CompletedProcess(
                args=["ansible"],
                returncode=0,
                stdout="",
                stderr="",
            )
        return subprocess.CompletedProcess(
            args=["ansible"],
            returncode=1,
            stdout="",
            stderr="unexpected",
        )

    monkeypatch.setattr(orchestrator, "_ansible_shell", fake_ansible_shell)

    resolved = orchestrator._resolve_mutagen_auth(vm_name, vm_ip, auth_mode="vm_user")
    assert resolved == ("clawbox-91", "dev-pass")
    assert attempted_users == [vm_name]


def test_resolve_mutagen_auth_raises_after_vm_user_auth_fails(monkeypatch: pytest.MonkeyPatch):
    vm_name = "clawbox-91"
    vm_ip = "192.168.64.10"
    attempted_users: list[str] = []

    monkeypatch.setattr(
        orchestrator,
        "vm_user_credentials",
        lambda _vm_name, *, secrets_file: (_vm_name, "dev-pass"),
    )

    def fake_ansible_shell(
        target: str,
        shell_cmd: str,
        *,
        ansible_user: str,
        ansible_password: str,
        become: bool = False,
        inventory_path: str = "inventory/tart_inventory.py",
    ) -> subprocess.CompletedProcess[str]:
        attempted_users.append(ansible_user)
        assert target == vm_ip
        assert shell_cmd == "true"
        assert become is False
        assert inventory_path == f"{vm_ip},"
        return subprocess.CompletedProcess(
            args=["ansible"],
            returncode=1,
            stdout="",
            stderr="dev auth failed",
        )

    monkeypatch.setattr(orchestrator, "_ansible_shell", fake_ansible_shell)

    with pytest.raises(UserFacingError) as exc_info:
        orchestrator._resolve_mutagen_auth(vm_name, vm_ip, auth_mode="vm_user")
    assert "Could not establish guest SSH credentials for Mutagen sync setup" in str(exc_info.value)
    assert "attempted user: clawbox-91" in str(exc_info.value)
    assert "dev auth failed" in str(exc_info.value)
    assert attempted_users == [vm_name]


def test_resolve_mutagen_auth_uses_bootstrap_admin_when_requested(
    monkeypatch: pytest.MonkeyPatch,
):
    vm_ip = "192.168.64.10"
    attempted_users: list[str] = []

    def fake_ansible_shell(
        target: str,
        shell_cmd: str,
        *,
        ansible_user: str,
        ansible_password: str,
        become: bool = False,
        inventory_path: str = "inventory/tart_inventory.py",
    ) -> subprocess.CompletedProcess[str]:
        attempted_users.append(ansible_user)
        assert target == vm_ip
        assert shell_cmd == "true"
        assert become is False
        assert inventory_path == f"{vm_ip},"
        return subprocess.CompletedProcess(
            args=["ansible"],
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(orchestrator, "_ansible_shell", fake_ansible_shell)

    resolved = orchestrator._resolve_mutagen_auth(
        "clawbox-91",
        vm_ip,
        auth_mode="bootstrap_admin",
    )
    assert resolved == (orchestrator.BOOTSTRAP_ADMIN_USER, orchestrator.BOOTSTRAP_ADMIN_PASSWORD)
    assert attempted_users == [orchestrator.BOOTSTRAP_ADMIN_USER]


def test_ensure_mutagen_keypair_returns_existing_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(orchestrator, "STATE_DIR", tmp_path / ".clawbox" / "state")
    key_path = orchestrator._mutagen_key_path("clawbox-91")
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text("private", encoding="utf-8")
    key_path.with_suffix(".pub").write_text("public", encoding="utf-8")

    resolved = orchestrator._ensure_mutagen_keypair("clawbox-91")
    assert resolved == key_path


def test_ensure_mutagen_keypair_maps_generation_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(orchestrator, "STATE_DIR", tmp_path / ".clawbox" / "state")
    monkeypatch.setattr(
        orchestrator.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("missing ssh-keygen")),
    )
    with pytest.raises(UserFacingError, match="Command not found: ssh-keygen"):
        orchestrator._ensure_mutagen_keypair("clawbox-91")

    monkeypatch.setattr(
        orchestrator.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["ssh-keygen"], returncode=1, stdout="", stderr="boom"
        ),
    )
    with pytest.raises(UserFacingError, match="Could not generate Mutagen SSH key"):
        orchestrator._ensure_mutagen_keypair("clawbox-91")


def test_ensure_remote_mutagen_authorized_key_success_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("private", encoding="utf-8")
    key_path.with_suffix(".pub").write_text("ssh-ed25519 AAAATEST", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "_ensure_mutagen_keypair", lambda _vm_name: key_path)

    seen_cmds: list[str] = []
    monkeypatch.setattr(
        orchestrator,
        "_ansible_shell",
        lambda _target, shell_cmd, **_kwargs: seen_cmds.append(shell_cmd)
        or subprocess.CompletedProcess(args=["ansible"], returncode=0, stdout="", stderr=""),
    )

    orchestrator._ensure_remote_mutagen_authorized_key(
        "clawbox-91",
        "192.168.64.10",
        ansible_user="clawbox-91",
        ansible_password="pw",
    )
    assert seen_cmds
    assert "authorized_keys" in seen_cmds[0]

    monkeypatch.setattr(
        orchestrator,
        "_ansible_shell",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["ansible"], returncode=1, stdout="", stderr="denied"
        ),
    )
    with pytest.raises(UserFacingError, match="Could not install Mutagen SSH key"):
        orchestrator._ensure_remote_mutagen_authorized_key(
            "clawbox-91",
            "192.168.64.10",
            ansible_user="clawbox-91",
            ansible_password="pw",
        )


def test_prepare_remote_mutagen_targets_maps_failure(monkeypatch: pytest.MonkeyPatch):
    specs = [
        orchestrator.SessionSpec(
            kind="openclaw-source",
            host_path=Path("/tmp/source"),
            guest_path="/Users/Shared/clawbox-sync/openclaw-source",
        ),
        orchestrator.SessionSpec(
            kind="openclaw-payload",
            host_path=Path("/tmp/payload"),
            guest_path="/Users/Shared/clawbox-sync/openclaw-payload",
        ),
        orchestrator.SessionSpec(
            kind="signal-payload",
            host_path=Path("/tmp/signal"),
            guest_path="/Users/Shared/clawbox-sync/signal-cli-payload",
        ),
    ]
    seen_cmds: list[str] = []
    monkeypatch.setattr(
        orchestrator,
        "_ansible_shell",
        lambda _target, shell_cmd, **_kwargs: seen_cmds.append(shell_cmd)
        or subprocess.CompletedProcess(args=["ansible"], returncode=0, stdout="", stderr=""),
    )
    orchestrator._prepare_remote_mutagen_targets(
        "192.168.64.10",
        specs,
        ansible_user="clawbox-91",
        ansible_password="pw",
    )
    assert seen_cmds
    assert 'if [ -L "$path" ]; then rm "$path"; fi' in seen_cmds[0]
    assert 'chmod -R a+rwX "$path"' not in seen_cmds[0]
    assert 'chmod -R u+rwX,g+rwX,o+rX "$path"' in seen_cmds[0]
    assert 'chmod -R o-w "$path"' in seen_cmds[0]
    assert seen_cmds[0].count('chmod -R u+rwX,g+rwX,o+rX "$path"') == len(specs)
    assert seen_cmds[0].count('chmod -R o-w "$path"') == len(specs)

    monkeypatch.setattr(
        orchestrator,
        "_ansible_shell",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["ansible"], returncode=1, stdout="", stderr="bad"
        ),
    )
    with pytest.raises(UserFacingError, match="Could not prepare guest directories"):
        orchestrator._prepare_remote_mutagen_targets(
            "192.168.64.10",
            specs,
            ansible_user="clawbox-91",
            ansible_password="pw",
        )


def test_activate_mutagen_sync_requires_running_vm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "source"
    payload = tmp_path / "payload"
    source.mkdir(parents=True, exist_ok=True)
    payload.mkdir(parents=True, exist_ok=True)
    tart = FakeTart()
    tart.running["clawbox-91"] = False
    monkeypatch.setattr(orchestrator, "mutagen_available", lambda: True)

    with pytest.raises(UserFacingError, match="must be running before activating Mutagen sync"):
        orchestrator._activate_mutagen_sync(
            vm_name="clawbox-91",
            openclaw_source=str(source),
            openclaw_payload=str(payload),
            signal_payload="",
            tart=tart,
            auth_mode="bootstrap_admin",
        )


def test_activate_mutagen_sync_success_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "source"
    payload = tmp_path / "payload"
    source.mkdir(parents=True, exist_ok=True)
    payload.mkdir(parents=True, exist_ok=True)

    tart = FakeTart()
    tart.running["clawbox-91"] = True
    monkeypatch.setattr(orchestrator, "mutagen_available", lambda: True)
    monkeypatch.setattr(orchestrator, "_resolve_vm_ip", lambda *_args, **_kwargs: "192.168.64.10")
    monkeypatch.setattr(
        orchestrator,
        "_resolve_mutagen_auth",
        lambda *_args, **_kwargs: ("clawbox-91", "pw"),
    )
    monkeypatch.setattr(orchestrator, "_ensure_remote_mutagen_authorized_key", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_ensure_mutagen_keypair", lambda _vm_name: tmp_path / "id_ed25519")
    monkeypatch.setattr(orchestrator, "_prepare_remote_mutagen_targets", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "ensure_mutagen_ssh_alias",
        lambda *_args, **_kwargs: "clawbox-mutagen-clawbox-91",
    )
    monkeypatch.setattr(orchestrator, "ensure_vm_sessions", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "_wait_for_mutagen_sync_ready",
        lambda *_args, **_kwargs: ["/Users/Shared/clawbox-sync/signal-cli-payload/marker-missing"],
    )
    marked: list[str] = []
    monkeypatch.setattr(orchestrator, "mark_mutagen_vm_active", lambda _state, vm: marked.append(vm))

    out = _capture_stdout(
        lambda: orchestrator._activate_mutagen_sync(
            vm_name="clawbox-91",
            openclaw_source=str(source),
            openclaw_payload=str(payload),
            signal_payload="",
            tart=tart,
            auth_mode="vm_user",
        )
    )
    assert "Preparing Mutagen sync..." in out
    assert "optional sync paths still warming up (continuing):" in out
    assert "Mutagen sync active (bidirectional):" in out
    assert marked == ["clawbox-91"]


def test_activate_mutagen_sync_from_locks_requires_source_and_payload(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        orchestrator,
        "_host_paths_from_locks",
        lambda _vm_name: {
            orchestrator.OPENCLAW_SOURCE_LOCK: "",
            orchestrator.OPENCLAW_PAYLOAD_LOCK: "",
            orchestrator.SIGNAL_PAYLOAD_LOCK: "",
        },
    )
    with pytest.raises(UserFacingError, match="Could not determine developer source/payload host paths"):
        orchestrator._activate_mutagen_sync_from_locks("clawbox-91", FakeTart())


def test_activate_mutagen_sync_from_locks_forwards_expected_values(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        orchestrator,
        "_host_paths_from_locks",
        lambda _vm_name: {
            orchestrator.OPENCLAW_SOURCE_LOCK: "/tmp/source",
            orchestrator.OPENCLAW_PAYLOAD_LOCK: "/tmp/payload",
            orchestrator.SIGNAL_PAYLOAD_LOCK: "/tmp/signal",
        },
    )
    seen: dict[str, object] = {}
    monkeypatch.setattr(orchestrator, "_activate_mutagen_sync", lambda **kwargs: seen.update(kwargs))

    orchestrator._activate_mutagen_sync_from_locks("clawbox-91", FakeTart())
    assert seen["vm_name"] == "clawbox-91"
    assert seen["openclaw_source"] == "/tmp/source"
    assert seen["openclaw_payload"] == "/tmp/payload"
    assert seen["signal_payload"] == "/tmp/signal"
    assert seen["auth_mode"] == "bootstrap_admin"


def test_deactivate_mutagen_sync_maps_mutagen_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        orchestrator,
        "teardown_vm_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(orchestrator.MutagenError("mutagen bad")),
    )
    with pytest.raises(UserFacingError, match="mutagen bad"):
        orchestrator._deactivate_mutagen_sync("clawbox-91", flush=False)


def test_build_sync_specs_ignores_node_modules_and_dist_for_source() -> None:
    specs = orchestrator._build_sync_specs(
        "clawbox-91",
        {
            orchestrator.OPENCLAW_SOURCE_LOCK: "/tmp/source",
            orchestrator.OPENCLAW_PAYLOAD_LOCK: "/tmp/payload",
            orchestrator.SIGNAL_PAYLOAD_LOCK: "",
        },
    )
    source_specs = [spec for spec in specs if spec.kind == "openclaw-source"]
    assert len(source_specs) == 1
    assert source_specs[0].ignored_paths == ("node_modules", "dist")
    assert source_specs[0].ready_required is True
    signal_specs = [spec for spec in specs if spec.kind == "signal-payload"]
    assert len(signal_specs) == 0


def test_build_sync_specs_requires_signal_ready_when_configured() -> None:
    specs = orchestrator._build_sync_specs(
        "clawbox-91",
        {
            orchestrator.OPENCLAW_SOURCE_LOCK: "/tmp/source",
            orchestrator.OPENCLAW_PAYLOAD_LOCK: "/tmp/payload",
            orchestrator.SIGNAL_PAYLOAD_LOCK: "/tmp/signal",
        },
    )
    signal_specs = [spec for spec in specs if spec.kind == "signal-payload"]
    assert len(signal_specs) == 1
    assert signal_specs[0].ready_required is True


def test_wait_for_mutagen_sync_ready_success_cleans_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "source"
    payload_dir = tmp_path / "payload"
    source_dir.mkdir(parents=True, exist_ok=True)
    payload_dir.mkdir(parents=True, exist_ok=True)
    specs = [
        orchestrator.SessionSpec(
            kind="openclaw-source",
            host_path=source_dir,
            guest_path="/Users/Shared/clawbox-sync/openclaw-source",
        ),
        orchestrator.SessionSpec(
            kind="openclaw-payload",
            host_path=payload_dir,
            guest_path="/Users/Shared/clawbox-sync/openclaw-payload",
        ),
    ]

    monkeypatch.setattr(
        orchestrator,
        "_parse_mount_statuses",
        lambda _stdout, paths: {path: "ok" for path in paths},
    )
    monkeypatch.setattr(
        orchestrator,
        "_ansible_shell",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["ansible"], returncode=0, stdout="", stderr=""
        ),
    )

    optional_missing = orchestrator._wait_for_mutagen_sync_ready(
        "192.168.64.10",
        specs,
        ansible_user="clawbox-91",
        ansible_password="clawbox",
        timeout_seconds=2,
    )
    assert optional_missing == []
    assert not list(source_dir.glob(".clawbox-sync-ready-*"))
    assert not list(payload_dir.glob(".clawbox-sync-ready-*"))


def test_wait_for_mutagen_sync_ready_timeout_cleans_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    specs = [
        orchestrator.SessionSpec(
            kind="openclaw-source",
            host_path=source_dir,
            guest_path="/Users/Shared/clawbox-sync/openclaw-source",
        )
    ]

    monkeypatch.setattr(orchestrator.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "_parse_mount_statuses",
        lambda _stdout, paths: {path: "missing" for path in paths},
    )
    monkeypatch.setattr(
        orchestrator,
        "_ansible_shell",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["ansible"], returncode=1, stdout="", stderr="probe failed"
        ),
    )

    with pytest.raises(UserFacingError, match="Mutagen sync did not become ready before timeout"):
        orchestrator._wait_for_mutagen_sync_ready(
            "192.168.64.10",
            specs,
            ansible_user="clawbox-91",
            ansible_password="clawbox",
            timeout_seconds=2,
        )
    assert not list(source_dir.glob(".clawbox-sync-ready-*"))


def test_wait_for_mutagen_sync_ready_timeout_includes_mutagen_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    specs = [
        orchestrator.SessionSpec(
            kind="openclaw-source",
            host_path=source_dir,
            guest_path="/Users/Shared/clawbox-sync/openclaw-source",
        )
    ]

    monkeypatch.setattr(orchestrator.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "_parse_mount_statuses",
        lambda _stdout, paths: {path: "missing" for path in paths},
    )
    monkeypatch.setattr(
        orchestrator,
        "_ansible_shell",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["ansible"], returncode=1, stdout="", stderr="probe failed"
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "vm_sessions_status",
        lambda vm_name: f"mutagen status for {vm_name}",
    )

    with pytest.raises(UserFacingError) as exc_info:
        orchestrator._wait_for_mutagen_sync_ready(
            "192.168.64.10",
            specs,
            vm_name="clawbox-91",
            ansible_user="clawbox-91",
            ansible_password="clawbox",
            timeout_seconds=2,
        )
    msg = str(exc_info.value)
    assert "mutagen session diagnostics" in msg
    assert "mutagen status for clawbox-91" in msg


def test_wait_for_mutagen_sync_ready_allows_optional_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "source"
    signal_dir = tmp_path / "signal"
    source_dir.mkdir(parents=True, exist_ok=True)
    signal_dir.mkdir(parents=True, exist_ok=True)
    specs = [
        orchestrator.SessionSpec(
            kind="openclaw-source",
            host_path=source_dir,
            guest_path="/Users/Shared/clawbox-sync/openclaw-source",
            ready_required=True,
        ),
        orchestrator.SessionSpec(
            kind="signal-payload",
            host_path=signal_dir,
            guest_path="/Users/Shared/clawbox-sync/signal-cli-payload",
            ready_required=False,
        ),
    ]

    def parse_statuses(_stdout: str, paths: list[str]) -> dict[str, str]:
        return {
            path: (
                "ok"
                if "openclaw-source/.clawbox-sync-ready-openclaw-source-" in path
                else "missing"
            )
            for path in paths
        }

    monkeypatch.setattr(orchestrator, "_parse_mount_statuses", parse_statuses)
    monkeypatch.setattr(
        orchestrator,
        "_ansible_shell",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["ansible"], returncode=1, stdout="", stderr="optional missing"
        ),
    )

    optional_missing = orchestrator._wait_for_mutagen_sync_ready(
        "192.168.64.10",
        specs,
        ansible_user="clawbox-91",
        ansible_password="clawbox",
        timeout_seconds=2,
    )
    assert len(optional_missing) == 1
    assert "signal-cli-payload/.clawbox-sync-ready-signal-payload-" in optional_missing[0]
    assert not list(source_dir.glob(".clawbox-sync-ready-*"))
    assert not list(signal_dir.glob(".clawbox-sync-ready-*"))


def test_preflight_developer_mounts_success(isolated_paths, monkeypatch: pytest.MonkeyPatch):
    payload_dir = isolated_paths / "payload"
    payload_dir.mkdir(parents=True, exist_ok=True)

    def fake_wait(*_args, **_kwargs):
        statuses = {path: "ok" for path in _kwargs["paths"]}
        return True, statuses, ""

    monkeypatch.setattr(orchestrator, "_wait_for_remote_probe", fake_wait)
    out = _capture_stdout(
        lambda: orchestrator._preflight_developer_mounts(
            "clawbox-91",
            vm_number=91,
            openclaw_payload_host=str(payload_dir),
            signal_payload_host="",
            include_signal_payload=False,
            timeout_seconds=3,
        )
    )
    assert "synced developer paths verified" in out


def test_preflight_developer_mounts_failure_contains_diagnostics(
    isolated_paths, monkeypatch: pytest.MonkeyPatch
):
    payload_dir = isolated_paths / "payload"
    payload_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        orchestrator,
        "_wait_for_remote_probe",
        lambda *_args, **_kwargs: (False, {path: "missing" for path in _kwargs["paths"]}, "boom"),
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_remote_path_probe",
        lambda *_args, **_kwargs: (
            0,
            {orchestrator.OPENCLAW_SOURCE_MOUNT: "missing", orchestrator.OPENCLAW_PAYLOAD_MOUNT: "missing"},
            "",
        ),
    )
    with pytest.raises(UserFacingError, match="Required synced developer paths failed preflight checks"):
        orchestrator._preflight_developer_mounts(
            "clawbox-91",
            vm_number=91,
            openclaw_payload_host=str(payload_dir),
            signal_payload_host="",
            include_signal_payload=False,
            timeout_seconds=3,
        )


def test_preflight_signal_payload_marker_success(monkeypatch: pytest.MonkeyPatch):
    marker_path = f"{orchestrator.SIGNAL_PAYLOAD_MOUNT}/{orchestrator.SIGNAL_PAYLOAD_MARKER_FILENAME}"
    monkeypatch.setattr(
        orchestrator,
        "_wait_for_remote_probe",
        lambda *_args, **_kwargs: (True, {marker_path: "ok"}, ""),
    )
    out = _capture_stdout(
        lambda: orchestrator._preflight_signal_payload_marker(
            "clawbox-91",
            vm_number=91,
            timeout_seconds=3,
            inventory_path="192.168.64.1,",
            target_host="192.168.64.1",
        )
    )
    assert "signal-cli payload marker verified" in out


def test_provision_vm_maps_missing_ansible_playbook(isolated_paths, monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    vm_name = "clawbox-91"
    tart.exists[vm_name] = True
    tart.running[vm_name] = True
    tart.ip_map[vm_name] = "192.168.64.10"

    monkeypatch.setattr(orchestrator, "ensure_secrets_file", lambda *_args, **_kwargs: None)

    def raise_not_found(*_args, **_kwargs):
        raise FileNotFoundError("missing ansible-playbook")

    monkeypatch.setattr(orchestrator.subprocess, "run", raise_not_found)
    with pytest.raises(UserFacingError, match="Command not found: ansible-playbook"):
        orchestrator.provision_vm(
            ProvisionOptions(
                vm_number=91,
                profile="standard",
                enable_playwright=False,
                enable_tailscale=False,
                enable_signal_cli=False,
                enable_signal_payload=False,
            ),
            tart,
        )


def test_stop_vm_and_wait_timeout(monkeypatch: pytest.MonkeyPatch):
    class NeverStops(FakeTart):
        def stop(self, vm_name: str) -> None:
            self.stop_calls.append(vm_name)
            self.running[vm_name] = True

    tart = NeverStops()
    tart.running["clawbox-91"] = True
    monkeypatch.setattr(orchestrator, "_deactivate_mutagen_sync", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda *_args, **_kwargs: None)
    assert orchestrator._stop_vm_and_wait(tart, "clawbox-91", timeout_seconds=2) is False


def test_stop_vm_and_wait_stops_watcher(monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    tart.running["clawbox-91"] = True
    stopped: list[str] = []
    monkeypatch.setattr(orchestrator, "_deactivate_mutagen_sync", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "stop_vm_watcher",
        lambda _state_dir, vm_name: stopped.append(vm_name) or True,
    )
    monkeypatch.setattr(orchestrator.time, "sleep", lambda *_args, **_kwargs: None)

    assert orchestrator._stop_vm_and_wait(tart, "clawbox-91", timeout_seconds=2) is True
    assert stopped == ["clawbox-91"]


def test_render_up_command_includes_optional_flags():
    cmd = orchestrator._render_up_command(
        UpOptions(
            vm_number=92,
            profile="developer",
            openclaw_source="/src",
            openclaw_payload="/payload",
            signal_payload="/signal",
            enable_playwright=True,
            enable_tailscale=True,
            enable_signal_cli=True,
        )
    )
    assert "--add-playwright-provisioning" in cmd
    assert "--add-tailscale-provisioning" in cmd
    assert "--add-signal-cli-provisioning" in cmd
    assert "--signal-cli-payload" in cmd


def test_compute_up_provision_reason_created_vm():
    opts = UpOptions(
        vm_number=91,
        profile="standard",
        openclaw_source="",
        openclaw_payload="",
        signal_payload="",
        enable_playwright=False,
        enable_tailscale=False,
        enable_signal_cli=False,
    )
    reason = orchestrator._compute_up_provision_reason(opts, Path("/tmp/nope"), True, False)
    assert reason == "VM was created in this run"


def test_compute_up_provision_reason_parse_failure(isolated_paths):
    marker_file = orchestrator.STATE_DIR / "clawbox-91.provisioned"
    marker_file.parent.mkdir(parents=True, exist_ok=True)
    marker_file.write_text("bad content\n", encoding="utf-8")
    opts = UpOptions(
        vm_number=91,
        profile="standard",
        openclaw_source="",
        openclaw_payload="",
        signal_payload="",
        enable_playwright=False,
        enable_tailscale=False,
        enable_signal_cli=False,
    )
    with pytest.raises(UserFacingError, match="could not be parsed"):
        orchestrator._compute_up_provision_reason(opts, marker_file, False, False)


def test_ensure_vm_running_for_up_timeout(monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    monkeypatch.setattr(orchestrator, "launch_vm", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "wait_for_vm_running", lambda *_args, **_kwargs: False)
    with pytest.raises(UserFacingError, match="did not transition to running state"):
        orchestrator._ensure_vm_running_for_up(
            "clawbox-91",
            UpOptions(
                vm_number=91,
                profile="standard",
                openclaw_source="",
                openclaw_payload="",
                signal_payload="",
                enable_playwright=False,
                enable_tailscale=False,
                enable_signal_cli=False,
            ),
            "needs provision",
            tart,
        )


def test_relaunch_gui_after_headless_provision_stop_timeout(monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    tart.running["clawbox-91"] = True
    monkeypatch.setattr(orchestrator, "_stop_vm_and_wait", lambda *_args, **_kwargs: False)
    with pytest.raises(UserFacingError, match="Timed out stopping headless VM"):
        orchestrator._relaunch_gui_after_headless_provision(
            "clawbox-91",
            UpOptions(
                vm_number=91,
                profile="standard",
                openclaw_source="",
                openclaw_payload="",
                signal_payload="",
                enable_playwright=False,
                enable_tailscale=False,
                enable_signal_cli=False,
            ),
            tart,
            launched_headless=True,
        )


def test_relaunch_gui_after_headless_provision_relaunch_timeout(monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    tart.running["clawbox-91"] = True
    monkeypatch.setattr(orchestrator, "_stop_vm_and_wait", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(orchestrator, "launch_vm", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "wait_for_vm_running", lambda *_args, **_kwargs: False)
    with pytest.raises(UserFacingError, match="after GUI relaunch"):
        orchestrator._relaunch_gui_after_headless_provision(
            "clawbox-91",
            UpOptions(
                vm_number=91,
                profile="standard",
                openclaw_source="",
                openclaw_payload="",
                signal_payload="",
                enable_playwright=False,
                enable_tailscale=False,
                enable_signal_cli=False,
            ),
            tart,
            launched_headless=True,
        )


def test_ensure_running_after_provision_launches(monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    calls = {"launch": 0, "wait": 0}

    def fake_wait(*_args, **_kwargs):
        calls["wait"] += 1
        return calls["wait"] > 1

    monkeypatch.setattr(orchestrator, "wait_for_vm_running", fake_wait)
    monkeypatch.setattr(orchestrator, "launch_vm", lambda *_args, **_kwargs: calls.update(launch=1))
    orchestrator._ensure_running_after_provision_if_needed(
        "clawbox-91",
        UpOptions(
            vm_number=91,
            profile="standard",
            openclaw_source="",
            openclaw_payload="",
            signal_payload="",
            enable_playwright=False,
            enable_tailscale=False,
            enable_signal_cli=False,
        ),
        tart,
        provision_ran=True,
    )
    assert calls["launch"] == 1


def test_up_errors_when_vm_missing_after_create(isolated_paths, monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    monkeypatch.setattr(orchestrator, "ensure_secrets_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "create_vm", lambda *_args, **_kwargs: None)
    with pytest.raises(UserFacingError, match="was not found after create_vm completed"):
        orchestrator.up(
            UpOptions(
                vm_number=91,
                profile="standard",
                openclaw_source="",
                openclaw_payload="",
                signal_payload="",
                enable_playwright=False,
                enable_tailscale=False,
                enable_signal_cli=False,
            ),
            tart,
        )


def test_up_errors_when_not_running_after_orchestration(isolated_paths, monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    tart.exists["clawbox-91"] = True
    monkeypatch.setattr(orchestrator, "ensure_secrets_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_compute_up_provision_reason", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(orchestrator, "_ensure_vm_running_for_up", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(orchestrator, "_ensure_running_after_provision_if_needed", lambda *_args, **_kwargs: None)
    with pytest.raises(UserFacingError, match="is not running after orchestration"):
        orchestrator.up(
            UpOptions(
                vm_number=91,
                profile="standard",
                openclaw_source="",
                openclaw_payload="",
                signal_payload="",
                enable_playwright=False,
                enable_tailscale=False,
                enable_signal_cli=False,
            ),
            tart,
        )


def test_up_mutagen_activates_sync(isolated_paths, monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    tart.exists["clawbox-91"] = True
    tart.running["clawbox-91"] = True
    marker = orchestrator.STATE_DIR / "clawbox-91.provisioned"
    marker.parent.mkdir(parents=True, exist_ok=True)
    orchestrator.ProvisionMarker(
        vm_name="clawbox-91",
        profile="developer",
        playwright=False,
        tailscale=False,
        signal_cli=False,
        signal_payload=False,
        provisioned_at="2026-01-01T00:00:00Z",
    ).write(marker)

    activated: list[str] = []
    monkeypatch.setattr(orchestrator, "ensure_secrets_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_compute_up_provision_reason", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(orchestrator, "_ensure_vm_running_for_up", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(orchestrator, "_ensure_running_after_provision_if_needed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "_activate_mutagen_sync",
        lambda **kwargs: activated.append(kwargs["vm_name"]),
    )

    out = _capture_stdout(
        lambda: orchestrator.up(
            UpOptions(
                vm_number=91,
                profile="developer",
                openclaw_source=str(isolated_paths),
                openclaw_payload=str(isolated_paths),
                signal_payload="",
                enable_playwright=False,
                enable_tailscale=False,
                enable_signal_cli=False,
            ),
            tart,
        )
    )
    assert activated == ["clawbox-91"]
    assert "Clawbox is running: clawbox-91 (provisioning skipped)" in out


def test_wait_for_vm_absent_timeout(monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    tart.exists["clawbox-91"] = True
    monkeypatch.setattr(orchestrator.time, "sleep", lambda *_args, **_kwargs: None)
    assert orchestrator._wait_for_vm_absent(tart, "clawbox-91", timeout_seconds=2) is False


def test_down_vm_nonexistent_cleans_locks(isolated_paths, monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    cleaned: list[str] = []
    monkeypatch.setattr(orchestrator, "cleanup_locks_for_vm", lambda vm_name: cleaned.append(vm_name))
    out = _capture_stdout(lambda: orchestrator.down_vm(91, tart))
    assert "does not exist" in out
    assert cleaned == ["clawbox-91"]


def test_down_vm_timeout_raises(monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    tart.exists["clawbox-91"] = True
    tart.running["clawbox-91"] = True
    monkeypatch.setattr(orchestrator, "_deactivate_mutagen_sync", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_stop_vm_and_wait", lambda *_args, **_kwargs: False)
    with pytest.raises(UserFacingError, match="Timed out waiting for VM 'clawbox-91' to stop"):
        orchestrator.down_vm(91, tart)


def test_down_vm_already_stopped_message(monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    tart.exists["clawbox-91"] = True
    tart.running["clawbox-91"] = False
    monkeypatch.setattr(orchestrator, "_deactivate_mutagen_sync", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "cleanup_locks_for_vm", lambda *_args, **_kwargs: None)
    out = _capture_stdout(lambda: orchestrator.down_vm(91, tart))
    assert "already stopped" in out


def test_delete_vm_nonexistent_cleans_state(isolated_paths, monkeypatch: pytest.MonkeyPatch):
    marker = orchestrator.STATE_DIR / "clawbox-91.provisioned"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("profile: standard\n", encoding="utf-8")
    tart = FakeTart()
    cleaned: list[str] = []
    monkeypatch.setattr(orchestrator, "cleanup_locks_for_vm", lambda vm_name: cleaned.append(vm_name))
    out = _capture_stdout(lambda: orchestrator.delete_vm(91, tart))
    assert "does not exist" in out
    assert not marker.exists()
    assert cleaned == ["clawbox-91"]


def test_delete_vm_timeout_before_delete(monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    tart.exists["clawbox-91"] = True
    tart.running["clawbox-91"] = True
    monkeypatch.setattr(orchestrator, "_deactivate_mutagen_sync", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_stop_vm_and_wait", lambda *_args, **_kwargs: False)
    with pytest.raises(UserFacingError, match="Timed out waiting for VM 'clawbox-91' to stop before deletion"):
        orchestrator.delete_vm(91, tart)


def test_delete_vm_still_exists_after_delete(monkeypatch: pytest.MonkeyPatch):
    tart = FakeTart()
    tart.exists["clawbox-91"] = True
    tart.running["clawbox-91"] = False
    monkeypatch.setattr(orchestrator, "_deactivate_mutagen_sync", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_wait_for_vm_absent", lambda *_args, **_kwargs: False)
    with pytest.raises(UserFacingError, match="still exists after delete attempt"):
        orchestrator.delete_vm(91, tart)


def test_ip_vm_errors_when_ip_unavailable():
    tart = FakeTart()
    tart.exists["clawbox-91"] = True
    tart.running["clawbox-91"] = True
    tart.ip_map["clawbox-91"] = None
    with pytest.raises(UserFacingError, match="Could not resolve IP"):
        orchestrator.ip_vm(91, tart)
