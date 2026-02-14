# signal-cli Payload Sync

Clawbox supports a `signal-cli` payload mode for developer workflows:

- Host payload directory is mounted into the VM at `/Volumes/My Shared Files/signal-cli-payload`
- `signal-cli` runs against VM-local state at `~/.local/share/signal-cli`

## Why This Exists

`signal-cli` (Java) persists account state using Java NIO file-channel flush behavior (`java.nio.channels.FileChannel.force`). On Tart VirtioFS shared folders, this is not reliable for this workload. Running `signal-cli` directly against the mounted payload can produce `Inappropriate ioctl for device` errors.

To keep runtime reliable while still keeping host/VM payloads aligned, Clawbox uses:

1. Initial seed from mounted host payload -> VM-local `~/.local/share/signal-cli`
2. Continuous background sync from VM-local state -> mounted host payload

## What Clawbox Configures

When you provision with:

- `--add-signal-cli-provisioning`
- and `--signal-cli-payload <path>` (which enables payload mode)

`signal-cli` provisioning itself is supported in both profiles. Payload mode is developer-only.

Clawbox installs a root-managed LaunchDaemon in the VM:

- Label: `com.clawbox.signal-cli-payload-sync`
- Long-running process with a 10-second sync loop
- Runtime user: `clawbox-<number>`
- Sync direction: VM local -> mounted host payload
- Destination readiness check: sync runs only when a host marker file is visible at the mounted payload path
- The readiness marker is excluded from rsync/delete operations so sync control state is not removed by payload mirroring
- SIGTERM trap performs a final sync during graceful shutdown
- Sync daemon logs to `/tmp/com.clawbox.signal-cli-payload-sync.log`
- Repeated `rsync` failures are counted; after a threshold, the process exits so launchd restarts it

The daemon also sets `ExitTimeOut` so launchd gives it time to flush before force-killing it during service teardown.

## Manual Provision Safety Guard

For manual workflows (`clawbox create` -> `clawbox launch` -> `clawbox provision`), `clawbox provision` performs a preflight check before running Ansible when `--enable-signal-payload` is set:

- Required marker path in guest: `/Volumes/My Shared Files/signal-cli-payload/.clawbox-signal-payload-host-marker`
- If the marker is missing, provisioning fails early with an actionable error.
- This prevents the seed step (`rsync --delete`) from running against an unintended local directory when the shared payload mount is not actually attached.

## Operational Model

- No manual reconciliation is required in normal usage.
- VM changes are checkpointed back to the host payload automatically.
- If the VM is torn down, the host payload generally contains recent state from the latest sync interval.

## Single-Writer Locking

To prevent accidental double-writer corruption, Clawbox enforces a single running VM per `signal-cli` payload path when launching with `--signal-cli-payload`.

- Lock registry path on host: `~/.clawbox/locks/signal-payload`
- Lock key: SHA-256 of canonical payload path
- Lock metadata: owner VM, owner host, payload path, timestamp

If a lock exists but its owner VM is no longer running, Clawbox automatically reclaims the stale lock.
Lock enforcement is host-local only (it coordinates VMs on the same host machine, not across multiple hosts).

## Shutdown Behavior

On graceful macOS shutdown initiated inside the VM (Apple menu shutdown/restart), launchd sends `SIGTERM` to the sync daemon. The daemon traps that signal and performs a final `rsync` before exit.

If shutdown is not graceful (host crash/power loss, forced kill), only the latest completed periodic checkpoint is guaranteed.

## Reliability Notes

- This is not a transactional replication protocol.
- Worst case (e.g., abrupt host crash/power loss): you can lose changes made since the last successful sync interval.
- Use a single-writer model per payload path (do not run multiple VMs writing to the same `signal-cli` payload simultaneously).

## Troubleshooting

Use `clawbox status <n>` to inspect shared mount state and sync daemon logs.

If `/Volumes/My Shared Files/signal-cli-payload` exists but reports as not mounted (`dir`/`missing`), restart the VM through Clawbox so launch arguments are reapplied:

```bash
clawbox down <n>
clawbox up <n> --developer ... --signal-cli-payload <path>
```
