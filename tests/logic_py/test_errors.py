from __future__ import annotations

import subprocess

import pytest

from clawbox import errors
from clawbox.errors import UserFacingError
from clawbox.tart import TartError


class FakeTart:
    pass


def test_main_guard_handles_user_facing_error(monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr(errors, "TartClient", FakeTart)

    def run(_tart):
        raise UserFacingError("bad input")

    with pytest.raises(SystemExit) as exc_info:
        errors.main_guard(run)
    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert "bad input" in captured.err


def test_main_guard_handles_tart_error(monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr(errors, "TartClient", FakeTart)

    def run(_tart):
        raise TartError("tart exploded")

    with pytest.raises(SystemExit) as exc_info:
        errors.main_guard(run)
    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert "tart exploded" in captured.err


def test_main_guard_handles_file_not_found(monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr(errors, "TartClient", FakeTart)

    def run(_tart):
        raise FileNotFoundError("missing")

    with pytest.raises(SystemExit) as exc_info:
        errors.main_guard(run)
    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert "Command not found" in captured.err


def test_main_guard_handles_subprocess_error(monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr(errors, "TartClient", FakeTart)

    def run(_tart):
        raise subprocess.SubprocessError("subprocess fail")

    with pytest.raises(SystemExit) as exc_info:
        errors.main_guard(run)
    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert "Command execution failed" in captured.err


def test_main_guard_handles_oserror(monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr(errors, "TartClient", FakeTart)

    def run(_tart):
        raise OSError("os fail")

    with pytest.raises(SystemExit) as exc_info:
        errors.main_guard(run)
    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert "OS command failure" in captured.err
