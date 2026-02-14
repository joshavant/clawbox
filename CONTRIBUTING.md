# Contributing to Clawbox

Thanks for your interest in contributing.

## Before You Open a PR

1. Create a branch from `main`.
2. Make your changes.
3. Run local checks.
4. Open a pull request with a Conventional Commit title.

## Local Checks (Same Commands CI Uses)

Recommended pre-PR command:

```bash
./scripts/pr prepare
```

This runs:

1. `fast`
2. `logic`

You can run tiers directly:

```bash
./scripts/ci/bootstrap.sh fast
./scripts/ci/run.sh fast

./scripts/ci/bootstrap.sh logic
./scripts/ci/run.sh logic
```

For VM integration checks (macOS + Tart required):

```bash
./scripts/ci/bootstrap.sh integration
./scripts/ci/run.sh integration
```

## Pull Request Requirements

Every pull request must pass:

1. `PR Policy` (Conventional Commit PR title)
2. `Fast Checks`
3. `Logic Checks`

Conventional Commit examples:

1. `feat: add optional service flag validation`
2. `fix: handle missing VM marker in up flow`
3. `docs: clarify standard vs developer modes`

## Integration Test Policy

Integration runs are intentionally opt-in because they are expensive.

Supported PR triggers:

1. Label `ci:integration-smoke` for smoke integration
2. Label `ci:integration-full` for broader integration
3. Maintainer PR comment commands:
`/ci integration smoke` or `/ci integration full`

Full CI behavior matrix:

1. `docs/ci-matrix.md`
2. `docs/ci-testing-strategy.md`

## Changelog and Releases

`CHANGELOG.md` is maintainer-managed.
Contributors should not add release entries unless explicitly asked.

Releases are cut from GitHub Actions `workflow_dispatch` (`Release` workflow), not local scripts.

Maintainer release setup requirement:

1. Configure repository secret `HOMEBREW_TAP_PAT`
2. Secret must be a fine-grained GitHub token with `contents:write` on `joshavant/homebrew-tap`

Release metadata requirements:

1. Tag format: `vX.Y.Z`
2. `pyproject.toml` `[project].version` matches tag without `v`
3. `CHANGELOG.md` contains heading `## vX.Y.Z`

## Architecture Notes

- Runtime orchestration code is Python-first in `clawbox/`.
- Runtime command surface is `clawbox <subcommand>`.
- Integration scenario orchestration lives in `tests/integration_py/run_integration.py`.

## Branch Protection (Maintainers)

Recommended branch protection checks on `main`:

1. `PR Policy`
2. `Fast Checks`
3. `Logic Checks`
