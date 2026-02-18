from __future__ import annotations

import json
from pathlib import Path

import pytest

from clawbox import sync_events


def _read_json_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_emit_sync_event_appends_structured_json(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    sync_events.emit_sync_event(
        state_dir,
        "clawbox-1",
        event="activate_ok",
        actor="orchestrator",
        reason="launch_vm",
        details={"flush": False},
    )

    path = state_dir / "logs" / "sync-events.jsonl"
    assert path.exists()
    records = _read_json_lines(path)
    assert len(records) == 1
    assert records[0]["vm"] == "clawbox-1"
    assert records[0]["event"] == "activate_ok"
    assert records[0]["actor"] == "orchestrator"
    assert records[0]["reason"] == "launch_vm"
    assert records[0]["details"] == {"flush": False}
    assert isinstance(records[0]["timestamp"], str)


def test_emit_sync_event_rotates_when_size_limit_exceeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    log_dir = state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    active = log_dir / "sync-events.jsonl"
    active.write_text("x" * 80, encoding="utf-8")
    monkeypatch.setenv("CLAWBOX_SYNC_EVENT_LOG_MAX_BYTES", "64")

    sync_events.emit_sync_event(
        state_dir,
        "clawbox-1",
        event="teardown_ok",
        actor="orchestrator",
        reason="down_vm",
        details=None,
    )

    rotated = log_dir / "sync-events.jsonl.1"
    assert rotated.exists()
    assert rotated.read_text(encoding="utf-8") == "x" * 80

    active_records = _read_json_lines(active)
    assert len(active_records) == 1
    assert active_records[0]["event"] == "teardown_ok"

