from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_SYNC_EVENT_LOG_MAX_BYTES_ENV = "CLAWBOX_SYNC_EVENT_LOG_MAX_BYTES"
_DEFAULT_SYNC_EVENT_LOG_MAX_BYTES = 5 * 1024 * 1024
_SYNC_EVENT_LOG_FILE = "sync-events.jsonl"
_SYNC_EVENT_LOG_ROTATED_FILE = "sync-events.jsonl.1"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _max_log_size_bytes() -> int:
    raw = os.getenv(_SYNC_EVENT_LOG_MAX_BYTES_ENV)
    if raw is None:
        return _DEFAULT_SYNC_EVENT_LOG_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_SYNC_EVENT_LOG_MAX_BYTES
    if value <= 0:
        return _DEFAULT_SYNC_EVENT_LOG_MAX_BYTES
    return value


def _log_path(state_dir: Path) -> Path:
    return state_dir / "logs" / _SYNC_EVENT_LOG_FILE


def _rotated_log_path(state_dir: Path) -> Path:
    return state_dir / "logs" / _SYNC_EVENT_LOG_ROTATED_FILE


def _maybe_rotate(path: Path, rotated: Path) -> None:
    if not path.exists():
        return
    if path.stat().st_size < _max_log_size_bytes():
        return
    rotated.unlink(missing_ok=True)
    path.replace(rotated)


def emit_sync_event(
    state_dir: Path,
    vm_name: str,
    *,
    event: str,
    actor: str,
    reason: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    """Append a best-effort structured sync lifecycle event to a local log."""
    try:
        path = _log_path(state_dir)
        rotated = _rotated_log_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        _maybe_rotate(path, rotated)

        payload: dict[str, Any] = {
            "timestamp": _timestamp(),
            "vm": vm_name,
            "event": event,
            "actor": actor,
            "reason": reason,
        }
        if details:
            payload["details"] = dict(details)
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")

        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, encoded)
        finally:
            os.close(fd)
    except OSError:
        # Event logging is diagnostic only and must not disrupt orchestration.
        return

