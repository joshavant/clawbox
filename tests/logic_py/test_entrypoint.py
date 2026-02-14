from __future__ import annotations

import runpy

from clawbox import main as main_module


def test_python_module_entrypoint_calls_main(monkeypatch):
    called = {"value": False}
    monkeypatch.setattr(main_module, "main", lambda: called.update(value=True))
    runpy.run_module("clawbox.__main__", run_name="__main__")
    assert called["value"] is True
