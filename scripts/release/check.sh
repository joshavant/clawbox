#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$PROJECT_DIR"

echo "==> release: fast checks"
./scripts/ci/run.sh fast

echo "==> release: logic checks"
./scripts/ci/run.sh logic

echo "==> release: exhaustive integration"
CLAWBOX_CI_EXHAUSTIVE=true ./scripts/ci/run.sh integration

echo "Release checklist passed."
