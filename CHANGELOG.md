# Changelog

All notable changes to this project are documented in this file.

## Unreleased

### Changes

- test: reserve automated test VM usage to `91-99` and enforce it with `tests/logic_py/test_vm_number_policy.py`
- test(logic): isolate runtime state for host-safe logic tests via temporary `HOME`/state paths
- test(status): move Mutagen status parser coverage to fixture-backed sample outputs
- test(integration): add `mutagen-contract` profile to verify `active` then `inactive/no active sessions found` status behavior
- ci: add manual-only integration workflow (`workflow_dispatch`) for Mutagen contract checks
- docs: update developer testing guidance for logic-vs-integration Mutagen boundaries and CI trigger split

## v1.2.3

### Changes

- feat(sync): add structured sync lifecycle event logging for activation/teardown diagnostics
- fix(sync): report Mutagen session health in `clawbox status` and warn when sessions are inactive
- fix(watcher): require consecutive not-running polls before watcher-triggered sync teardown
- test(integration): assert orchestrator and watcher teardown event sequences


## v1.2.2

### Changes

- docs: restore gateway command, now that OC is fixed
- fix: prevent wheel build regressions from stale package data


## v1.2.1

### Changes

- fix: add provisioning network preflight checks and test fault injection
- docs: add SSH example for standard mode
- docs: add default password


## v1.2.0

### Changes

- build: exclude dist/ from sync (#6)
- chore: improve gateway behavior + README instructions (#7)


## v1.1.0

### Changes

- feat(sync): migrate Clawbox VM shared paths to Mutagen and remove legacy folder mounting (#4)


## v1.0.2

### Changes

- fix(integration): remove stale provision markers during cleanup (#2)


## v1.0.1

### Changes

- build: run packer tart image builds headlessly (#1)


## v1.0.0

### Changes

- Initial release!
