#!/bin/bash
set -euo pipefail

MODE="${1:-all}"
OS="$(uname -s)"
BREW_UPDATED=0

usage() {
	local exit_code="${1:-1}"
	cat <<'USAGE'
Usage: ./scripts/ci/bootstrap.sh <mode>

Modes:
  fast         Install dependencies for fast checks
  logic        Install dependencies for logic checks
  integration  Install dependencies for integration checks
  all          Install dependencies for all checks
USAGE
	exit "$exit_code"
}

install_pytest_stack() {
	if python3 - <<'PY' >/dev/null 2>&1; then
import importlib.util
import subprocess
import sys
ok = (
    subprocess.call([sys.executable, "-m", "pytest", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
    and importlib.util.find_spec("pytest_cov") is not None
)
raise SystemExit(0 if ok else 1)
PY
		return
	fi

	if python3 -m pip install pytest pytest-cov >/dev/null 2>&1; then
		return
	fi

	python3 -m pip install --user --break-system-packages pytest pytest-cov
}

install_fast_deps_linux() {
	sudo apt-get update
	sudo apt-get install -y python3 python3-pip shellcheck shfmt yamllint rsync jq
	python3 -m pip install --upgrade pip
	python3 -m pip install ansible-core ansible-lint
	ansible-galaxy collection install community.general
	install_actionlint_linux
}

install_logic_deps_linux() {
	sudo apt-get update
	sudo apt-get install -y python3 python3-pip rsync
	install_pytest_stack
}

install_fast_deps_macos() {
	brew_update_once
	brew install ansible ansible-lint shellcheck shfmt yamllint jq actionlint
	ansible-galaxy collection install community.general
}

install_logic_deps_macos() {
	brew_update_once
	brew install rsync
	install_pytest_stack
}

install_integration_deps_macos() {
	brew_update_once
	brew install cirruslabs/cli/tart ansible
	ansible-galaxy collection install community.general
}

brew_update_once() {
	if [ "$BREW_UPDATED" -eq 1 ]; then
		return
	fi
	brew update
	BREW_UPDATED=1
}

install_actionlint_linux() {
	if command -v actionlint >/dev/null 2>&1; then
		return
	fi

	local version="1.7.7"
	local arch
	arch="$(uname -m)"
	case "$arch" in
	x86_64)
		arch="amd64"
		;;
	aarch64 | arm64)
		arch="arm64"
		;;
	*)
		echo "Error: Unsupported architecture for actionlint install: $arch"
		exit 1
		;;
	esac

	local tmp_dir
	tmp_dir="$(mktemp -d)"
	curl -fsSL "https://github.com/rhysd/actionlint/releases/download/v${version}/actionlint_${version}_linux_${arch}.tar.gz" |
		tar -xz -C "$tmp_dir"
	sudo install -m 0755 "$tmp_dir/actionlint" /usr/local/bin/actionlint
	rm -rf "$tmp_dir"
}

case "$MODE" in
-h | --help)
	usage 0
	;;
fast)
	if [ "$OS" = "Darwin" ]; then
		install_fast_deps_macos
	else
		install_fast_deps_linux
	fi
	;;
logic)
	if [ "$OS" = "Darwin" ]; then
		install_logic_deps_macos
	else
		install_logic_deps_linux
	fi
	;;
integration)
	if [ "$OS" != "Darwin" ]; then
		echo "Error: integration dependencies require macOS"
		exit 1
	fi
	install_integration_deps_macos
	;;
all)
	if [ "$OS" = "Darwin" ]; then
		install_fast_deps_macos
		install_logic_deps_macos
		install_integration_deps_macos
	else
		install_fast_deps_linux
		install_logic_deps_linux
	fi
	;;
*)
	echo "Error: Unknown mode '$MODE'"
	usage 1
	;;
esac
