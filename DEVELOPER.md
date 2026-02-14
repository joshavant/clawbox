# Clawbox Developer Guide

This document is for contributors and maintainers working on Clawbox itself.
If you only want to use Clawbox, start with `README.md`.

## Development Scope

Clawbox is orchestration-only by design:

1. VM lifecycle and provisioning automation are in scope.
2. Mount wiring, isolation, and safety checks are in scope.
3. OpenClaw runtime behavior remains stock and first-party.

## Local Setup

Clawbox commands default to VM `1` when the number is omitted.

Recommended install from repository root:

```bash
brew install pipx
pipx install --editable .
```

Alternative (virtualenv):

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --editable .
```

Build the base image before first VM creation:

```bash
clawbox image build
```

## Typical Developer Run

```bash
clawbox up --developer \
  --openclaw-source ~/path/to/openclaw \
  --openclaw-payload ~/path/to/openclaw-payload
```

## Command Flows

Recommended entrypoint:

1. `clawbox up`

Manual component flow:

1. `clawbox create`
2. `clawbox launch`
3. `clawbox provision`

Use `clawbox launch --headless` when you want provisioning without opening a VM window.

## Profiles and Optional Services

`standard` profile:

1. Installs official OpenClaw release in the VM.
2. Does not use host source/payload mounts.
3. Supports optional provisioning flags:
`--add-playwright-provisioning`, `--add-tailscale-provisioning`, `--add-signal-cli-provisioning`.

`developer` profile:

1. Requires `--openclaw-source` and `--openclaw-payload`.
2. Installs dependencies from mounted source and runs the build gate (`pnpm exec tsdown`) during provisioning.
3. Links mounted source as the VM `openclaw` command.
4. Supports the same optional provisioning flags as `standard`.

`signal-cli` payload mode (developer-only):

1. Add `--signal-cli-payload <path>` to `clawbox up` or `clawbox launch`.
2. Also pass `--add-signal-cli-provisioning`.
3. For manual `clawbox provision`, also pass `--enable-signal-payload`.
4. Payload mode details: `docs/signal-cli-payload-sync.md`.

## Locking Model

Clawbox enforces single-writer, host-local locking for these paths:

1. `--openclaw-source`
2. `--openclaw-payload`
3. `--signal-cli-payload` (when used)

If an owning VM is no longer running, the lock is reclaimed automatically.
Locks are coordinated on one host only.

## Testing

Before opening a PR:

```bash
./scripts/pr prepare
```

This runs `fast` and `logic`.

Run tiers directly:

```bash
./scripts/ci/bootstrap.sh fast
./scripts/ci/run.sh fast

./scripts/ci/bootstrap.sh logic
./scripts/ci/run.sh logic
```

For integration details and CI trigger behavior:

1. `docs/ci-testing-strategy.md`
2. `docs/ci-matrix.md`

## Release Notes (Maintainers)

Releases are created from GitHub Actions `workflow_dispatch` (`Release` workflow), not local scripts.

Required repository secret before running release workflow:

1. `HOMEBREW_TAP_PAT`
2. Fine-grained GitHub token with `contents:write` access to `joshavant/homebrew-tap`

Release metadata requirements:

1. Tag format: `vX.Y.Z`
2. `pyproject.toml` version matches tag without `v`
3. `CHANGELOG.md` includes `## vX.Y.Z`

## Useful Commands

Recreate VM `1`:

```bash
clawbox recreate 1
```

Inspect VM `1`:

```bash
clawbox status 1
```

Inspect the full local Clawbox environment:

```bash
clawbox status
```
