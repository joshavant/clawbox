#!/usr/bin/env python3
"""
Dynamic Ansible inventory for tart macOS VMs.

Lists all running VMs matching the configured '<vm_base_name>-N' pattern
and resolves their IPs via `tart ip`. Assigns vm_number per host.

Usage:
  ansible-playbook -i inventory/tart_inventory.py playbooks/provision.yml

Supports standard Ansible dynamic inventory flags:
  --list   Print full inventory as JSON (default)
  --host   Print host-specific vars as JSON
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from clawbox.config import vm_base_name
from clawbox.tart import TartClient, TartError


def vm_pattern() -> re.Pattern[str]:
    base_name = vm_base_name()
    return re.compile(rf"^{re.escape(base_name)}-(\d+)$")


def get_tart_vms(tart: TartClient):
    """Return list of (vm_name, running) tuples from tart list."""
    vms = tart.list_vms_json()
    vm_rows = []
    for vm in vms:
        running = vm.get("Running")
        vm_name = vm.get("Name")
        if not isinstance(vm_name, str) or not vm_name:
            continue
        vm_rows.append((vm_name, bool(running) if isinstance(running, bool) else False))
    return vm_rows


def get_tart_ip(tart: TartClient, vm_name: str):
    """Resolve VM IP using agent resolver first, then default resolver."""
    return tart.ip(vm_name)


def build_inventory(tart: TartClient | None = None):
    inventory = {
        "_meta": {"hostvars": {}},
        "all": {"hosts": [], "vars": {"ansible_become": True}},
    }
    tart_client = tart or TartClient()

    pattern = vm_pattern()
    for vm_name, running in get_tart_vms(tart_client):
        match = pattern.match(vm_name)
        if not match:
            continue
        if not running:
            continue

        vm_number = int(match.group(1))
        ip = get_tart_ip(tart_client, vm_name)
        if not ip:
            print(f"Warning: Could not resolve IP for {vm_name}, skipping", file=sys.stderr)
            continue

        inventory["all"]["hosts"].append(vm_name)
        inventory["_meta"]["hostvars"][vm_name] = {
            "ansible_host": ip,
            "vm_number": vm_number,
        }

    return inventory


def host_vars(hostname):
    """Return vars for a single host."""
    inventory = build_inventory()
    return inventory["_meta"]["hostvars"].get(hostname, {})


def main():
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--host":
            hostname = sys.argv[2] if len(sys.argv) > 2 else ""
            print(json.dumps(host_vars(hostname), indent=2))
        else:
            print(json.dumps(build_inventory(), indent=2))
    except TartError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
