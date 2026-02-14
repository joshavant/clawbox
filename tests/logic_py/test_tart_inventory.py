from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from clawbox.tart import TartError


def _load_inventory_module():
    module_path = Path(__file__).resolve().parents[2] / "ansible" / "inventory" / "tart_inventory.py"
    spec = importlib.util.spec_from_file_location("tart_inventory_test_module", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeTart:
    def __init__(self, vm_rows: list[dict[str, object]], ip_map: dict[str, str | None]):
        self._vm_rows = vm_rows
        self._ip_map = ip_map

    def list_vms_json(self):
        return self._vm_rows

    def ip(self, vm_name: str):
        return self._ip_map.get(vm_name)


def test_build_inventory_uses_tart_client_data():
    inventory_mod = _load_inventory_module()
    tart = FakeTart(
        vm_rows=[
            {"Name": "clawbox-1", "Running": True},
            {"Name": "clawbox-2", "Running": False},
            {"Name": "other-vm", "Running": True},
        ],
        ip_map={"clawbox-1": "192.168.64.10"},
    )

    inventory = inventory_mod.build_inventory(tart=tart)

    assert inventory["all"]["hosts"] == ["clawbox-1"]
    assert inventory["_meta"]["hostvars"]["clawbox-1"]["ansible_host"] == "192.168.64.10"
    assert inventory["_meta"]["hostvars"]["clawbox-1"]["vm_number"] == 1


def test_get_tart_vms_handles_tart_error():
    inventory_mod = _load_inventory_module()

    class FailingTart:
        def list_vms_json(self):
            raise TartError("no tart")

    with pytest.raises(TartError, match="no tart"):
        inventory_mod.get_tart_vms(FailingTart())


def test_get_tart_ip_propagates_tart_error():
    inventory_mod = _load_inventory_module()

    class FailingTart:
        def ip(self, vm_name: str):
            raise TartError("no ip")

    with pytest.raises(TartError, match="no ip"):
        inventory_mod.get_tart_ip(FailingTart(), "clawbox-1")


def test_main_exits_nonzero_when_tart_errors(monkeypatch: pytest.MonkeyPatch, capsys):
    inventory_mod = _load_inventory_module()
    monkeypatch.setattr(inventory_mod, "build_inventory", lambda *args, **kwargs: (_ for _ in ()).throw(TartError("boom")))
    monkeypatch.setattr(sys, "argv", ["tart_inventory.py", "--list"])

    with pytest.raises(SystemExit) as exc_info:
        inventory_mod.main()

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert "Error: " in captured.err
