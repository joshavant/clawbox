#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_DIR"

echo "==> Shell syntax"
bash -n scripts/*.sh scripts/pr scripts/ci/*.sh scripts/release/*.sh

echo "==> Python syntax"
python3 - <<'PY'
from pathlib import Path

for path in [Path("ansible/inventory/tart_inventory.py"), *Path("clawbox").glob("*.py"), *Path("tests/logic_py").glob("*.py")]:
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
for path in Path("tests/integration_py").glob("*.py"):
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
PY

echo "==> Ansible playbook syntax"
(
	cd ansible
	ansible-playbook -i localhost, playbooks/provision.yml --syntax-check
)

if command -v shellcheck >/dev/null 2>&1; then
	echo "==> shellcheck"
	shellcheck -x -e SC1091 scripts/*.sh scripts/pr scripts/ci/*.sh scripts/release/*.sh
else
	echo "==> shellcheck (skipped: not installed)"
fi

if command -v ansible-lint >/dev/null 2>&1; then
	echo "==> ansible-lint"
	(
		cd ansible
		ansible-lint playbooks/provision.yml
	)
else
	echo "==> ansible-lint (skipped: not installed)"
fi

echo "Validation passed."
