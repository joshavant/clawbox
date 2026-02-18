#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
MODE="${1:-all}"

usage() {
	local exit_code="${1:-1}"
	cat <<'USAGE'
Usage: ./scripts/ci/run.sh <mode>

Modes:
  fast         Run static and lint checks
  logic        Run hermetic logic tests for Python orchestration
  integration  Run real Tart integration checks (macOS only)
  all          Run fast + logic + integration
USAGE
	exit "$exit_code"
}

require_cmd() {
	local cmd="$1"
	if ! command -v "$cmd" >/dev/null 2>&1; then
		echo "Error: Required command not found: $cmd"
		echo "Tip: install CI dependencies (see docs/ci-testing-strategy.md)."
		exit 1
	fi
}

run_fast() {
	require_cmd bash
	require_cmd python3
	require_cmd ansible-playbook
	require_cmd shellcheck
	require_cmd ansible-lint
	require_cmd shfmt
	require_cmd yamllint
	require_cmd actionlint
	require_cmd rsync

	echo "==> validate.sh"
	./scripts/validate.sh

	run_packaging_smoke

	echo "==> shfmt"
	shfmt -d scripts

	echo "==> yamllint"
	yamllint .github/workflows ansible .cirrus.yml

	echo "==> actionlint"
	actionlint

	echo "Fast checks passed."
}

run_packaging_smoke() {
	echo "==> packaging smoke (wheel build)"

	if ! python3 -m pip --version >/dev/null 2>&1; then
		echo "Error: python3 -m pip is required for packaging smoke checks."
		exit 1
	fi

	(
		set -euo pipefail
		local tmp_source
		local tmp_wheels
		tmp_source="$(mktemp -d "${TMPDIR:-/tmp}/clawbox-wheel-src.XXXXXX")"
		tmp_wheels="$(mktemp -d "${TMPDIR:-/tmp}/clawbox-wheel-out.XXXXXX")"
		trap 'rm -rf "$tmp_source" "$tmp_wheels"' EXIT

		rsync -a \
			--exclude '.git' \
			--exclude '.venv' \
			--exclude '.pytest_cache' \
			--exclude '__pycache__' \
			--exclude '.mypy_cache' \
			--exclude 'build' \
			--exclude 'dist' \
			"$PROJECT_DIR/" "$tmp_source/"

		python3 -m pip wheel --no-deps "$tmp_source" -w "$tmp_wheels"
	)
}

run_cli_smoke() {
	echo "==> clawbox CLI smoke"
	python3 - <<'PY'
import subprocess
import sys

base_help = subprocess.check_output([sys.executable, "-m", "clawbox", "--help"], text=True)
required = {"create", "launch", "provision", "up", "down", "delete", "ip", "status", "image"}
missing = [name for name in sorted(required) if name not in base_help]
if missing:
    raise SystemExit(f"Missing top-level clawbox subcommands: {', '.join(missing)}")

image_help = subprocess.check_output([sys.executable, "-m", "clawbox", "image", "--help"], text=True)
for name in ("init", "build", "rebuild"):
    if name not in image_help:
        raise SystemExit(f"Missing clawbox image subcommand: {name}")
PY
}

run_logic() {
	require_cmd python3
	echo "==> logic tests"
	local coverage_json
	coverage_json="$(mktemp "${TMPDIR:-/tmp}/clawbox-logic-coverage.XXXXXX")"
	python3 -m pytest -q tests/logic_py \
		--cov=clawbox \
		--cov-report=term-missing \
		--cov-report="json:${coverage_json}" \
		--cov-fail-under=85
	python3 - "$coverage_json" <<'PY'
import json
import sys
from pathlib import Path

coverage_path = Path(sys.argv[1])
payload = json.loads(coverage_path.read_text(encoding="utf-8"))

critical_thresholds = {
    "clawbox/ansible_exec.py": 85.0,
    "clawbox/errors.py": 85.0,
    "clawbox/orchestrator.py": 85.0,
    "clawbox/tart.py": 80.0,
}

missing = []
failing = []
for module, minimum in critical_thresholds.items():
    summary = payload.get("files", {}).get(module, {}).get("summary")
    if not summary:
        missing.append(module)
        continue
    actual = float(summary.get("percent_covered", 0.0))
    if actual < minimum:
        failing.append((module, actual, minimum))

if missing:
    print("Missing module coverage entries:", file=sys.stderr)
    for module in missing:
        print(f"  - {module}", file=sys.stderr)
if failing:
    print("Critical module coverage below threshold:", file=sys.stderr)
    for module, actual, minimum in failing:
        print(f"  - {module}: {actual:.2f}% < {minimum:.2f}%", file=sys.stderr)

if missing or failing:
    raise SystemExit(1)
PY
	run_cli_smoke
	echo "Logic checks passed."
}

run_integration() {
	PYTHONPATH="$PROJECT_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 tests/integration_py/run_integration.py
}

cd "$PROJECT_DIR"

case "$MODE" in
-h | --help)
	usage 0
	;;
fast)
	run_fast
	;;
logic)
	run_logic
	;;
integration)
	run_integration
	;;
all)
	run_fast
	run_logic
	run_integration
	;;
*)
	echo "Error: Unknown mode '$MODE'"
	usage 1
	;;
esac
