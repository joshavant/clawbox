# signal-cli Payload Sync

Clawbox supports a `signal-cli` payload mode for developer workflows:

- Host payload directory is synchronized into the VM at `/Users/Shared/clawbox-sync/signal-cli-payload`
- VM runtime path `/Users/<vm>/.local/share/signal-cli` is a symlink to that synced payload directory

## What Clawbox Configures

When you provision with:

- `--add-signal-cli-provisioning`
- and `--signal-cli-payload <path>` (which enables payload mode)

`signal-cli` provisioning itself is supported in both profiles. Payload mode is developer-only.

Clawbox configures payload mode by:

1. validating the synced payload path and marker file,
2. replacing `/Users/<vm>/.local/share/signal-cli` with a symlink to `/Users/Shared/clawbox-sync/signal-cli-payload`,
3. relying on Mutagen bidirectional sync for host/VM updates.

In `clawbox up --developer` flows that include signal payload mode, Clawbox establishes Mutagen sync twice:

1. before provisioning starts (headless phase), and
2. after post-provision GUI relaunch.

In both phases, `signal-cli-payload` readiness is required before continuing so payload seeding/provisioning does not run against a stale or missing sync path.

## Manual Provision Safety Guard

For manual workflows (`clawbox create` -> `clawbox launch` -> `clawbox provision`), `clawbox provision` performs a preflight check before running Ansible when `--enable-signal-payload` is set:

- Required marker path in guest: `/Users/Shared/clawbox-sync/signal-cli-payload/.clawbox-signal-payload-host-marker`
- If the marker is missing, provisioning fails early with an actionable error.
- This prevents payload mode from wiring `signal-cli` to an unintended local directory when the synced payload path is not actually ready.

## Operational Model

- No manual reconciliation is required in normal usage.
- VM and host changes flow through Mutagen bidirectional sync.
- If the VM is torn down, payload state reflects the latest completed Mutagen synchronization.

## Single-Writer Locking

To prevent accidental double-writer corruption, Clawbox enforces a single running VM per `signal-cli` payload path when launching with `--signal-cli-payload`.

- Lock registry path on host: `~/.clawbox/locks/signal-payload`
- Lock key: SHA-256 of canonical payload path
- Lock metadata: owner VM, owner host, payload path, timestamp

If a lock exists but its owner VM is no longer running, Clawbox automatically reclaims the stale lock.
Lock enforcement is host-local only (it coordinates VMs on the same host machine, not across multiple hosts).

## Reliability Notes

- This is not a transactional replication protocol.
- Worst case (e.g., abrupt host crash/power loss): you can lose changes made since the last successful synchronization cycle.
- Use a single-writer model per payload path (do not run multiple VMs writing to the same `signal-cli` payload simultaneously).

## Troubleshooting

Use `clawbox status <n>` to inspect synced path state.

If `/Users/Shared/clawbox-sync/signal-cli-payload` is missing expected marker/files, restart the VM through Clawbox so Mutagen sessions are re-established:

```bash
clawbox down <n>
clawbox up <n> --developer ... --signal-cli-payload <path>
```
