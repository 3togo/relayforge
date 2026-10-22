# Native live service handover

The native installer now has a separate, explicit live handover entry point.
It combines the existing durable payload transaction with service maintenance,
systemd activation exclusion, bounded shutdown, reload and verified startup.
The existing desktop D-Bus interface, policies and controls are unchanged.

This first implementation supports an empty installation or an already managed,
complete native installation of the same backend. Sharing must be idle. It never
calls hotspot Start, reconnects the upstream, resumes prior sharing, changes
enablement, installs packages or adopts an unmanaged predecessor. A running
Python service or older native daemon without the maintenance interface must be
stopped and adopted through a separately controlled offline procedure first.

The implementation has been tested with private-bus service-manager fixtures and
real Rust demo processes. Privileged host installation, real systemd/D-Bus
integration and physical handover remain release gates; no active host was changed
to establish these results.

## Explicit commands

Prepare a trusted, protected root-owned staged payload as described in
[offline deployment](native-deployment.md), and initialize or retain a private
valid configuration with the selected backend. The live root is permitted only
through this entry point, as effective UID 0:

```text
relayforge-service --deploy-live plan --root / --payload /srv/native-payload
relayforge-service --deploy-live install --root / --payload /srv/native-payload
relayforge-service --deploy-live status --root /
relayforge-service --deploy-live recover --root /
relayforge-service --deploy-live rollback --root /
```

`plan` validates the snapshot, payload, configuration and current native owner
without stopping or masking services or creating a deployment receipt. It is
advisory; installation repeats preflight under the deployment lock. An identical
installation is a no-op and preserves its rollback point. `status` reports live
receipt presence, phase and abort direction without contacting a bus.

Production connects directly to `/run/dbus/system_bus_socket`, ignoring bus-address
environment overrides. It pins the bus ID and systemd's unique owner, verifies
the manager UID, refuses active jobs, conflicting owners, administrator masks,
unit overrides, foreign executables and pending networking evidence. The current
service must belong to the expected systemd unit, with its main PID equal to the
bus owner's PID and the running ELF hash matching the recorded snapshot.

Live targets must contain the `relayforge:live-maintenance:v1` protocol marker;
the matching executable reports it with `--deployment-protocol`. This rejects
older binaries before activation, including old rollback targets. The marker is
a compatibility declaration, not a signature or proof that arbitrary code is
trustworthy: use only audited, trusted staged builds. Old payloads remain usable
through the separate offline workflow.

Offline `--deploy` still refuses `/`, mounts and symlinks. Live traversal remains
descriptor-relative with BENEATH and NO_SYMLINKS, but permits mount boundaries
needed by real `/run`, `/var` and `/usr` layouts. Ownership, permissions, regular
files, single links, metadata and fsync requirements remain enforced. Symlinked
directory layouts, security labels/ACLs/capabilities and distribution-specific
unit overrides require separate integration work.

## Handover sequence

1. Verify the payload, complete native source/target snapshots, private config,
   saved rollback blobs, unit definitions and idle service. Save all before/after
   blobs and a private `var/lib/relayforge-deploy/live.json` receipt before any
   service mutation. Hold the existing deployment lock throughout.
2. Use the separate `/io/github/relayforge/Deployment` object and
   `io.github.relayforge.Deployment.SetMaintenance(bool)` interface to block new
   Start, SetConfig, band-change and firewall requests. Only UID 0 can change it
   in production; ordinary-user demo fixtures permit their own UID. Status reads
   and explicit Stop remain available. The gate shares the normal operation and
   configuration locks, checks idle/sleep state and cancels resume intent.
3. Runtime-mask the four known compatibility/native service names with
   `MaskUnitFiles(runtime=true, force=false)` and reload systemd. Stop the
   recognized native generation, wait for no service jobs/process/bus owner,
   then acquire the native runtime lock and recheck networking evidence.
4. Apply the existing file transaction and commit its receipt; reload systemd
   and D-Bus configuration. Persist the starting phase before releasing the
   native runtime lock. Unmask only the target and request its start.
5. Verify the replacement's unit, executable hash, UID, main PID, bus ownership,
   maintenance gate and idle status. Native service startup sees a pending live
   receipt and enters maintenance before exposing desktop mutation methods.
   Restore runtime masks, persist ready state, release maintenance and remove
   the live receipt. Service enablement is never changed.

The fixed service allowlist is `wifi-hotspot-daemon.service`, `relayforge.service`,
`relayforge-native.service` and `relayforge-create-ap.service`. The controller uses
the [systemd manager D-Bus API](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.systemd1.html)
directly and does not spawn systemctl or shell helpers. Calls and shutdown/startup
waits are bounded, with an overall 240-second limit. A timeout leaves receipts
for explicit recovery; it never forces resource deletion or kills an unknown PID.

## Recovery

Before ready state, `recover` re-establishes maintenance and masks, stops only a
recognized original/target native generation, verifies runtime cleanup and
finishes the recorded file direction. Already committed file receipts are
recognized so retries do not swap rollback history twice. `rollback` while
pending durably selects abort before any restoration; subsequent recovery
continues that decision and retains the older rollback snapshot.

After ready state, recovery only finishes verification and maintenance release.
It does not stop a replacement that has already accepted a new desktop Start.
If an idle replacement exited before finalization, it may be restarted only
after confirming no owner and no networking evidence. A missing or externally
changed completed file receipt is refused. A ready handover must be finalized
with `recover` before requesting a new completed-deployment rollback.

Without pending live work, rollback selects the preceding complete native
snapshot or the empty pre-installation baseline, then runs the same handover.
The displaced snapshot becomes the next rollback target. Offline rollback remains
necessary for an unmanaged/partial predecessor. Configuration is never restored
from a payload backup or overwritten.

Bus/manager replacement or reboot requires administrator review; this version
does not silently rebind an outstanding transaction to a new manager. Keep the
matching installer available, state/blobs intact, and package managers and other
administrative writers quiescent. The deployment lock cannot serialize arbitrary
root writes. Filesystem power-loss, distribution boot/activation/authorization,
cross-version configuration and state compatibility, and physical Wi-Fi behavior
are not established by the private fixtures.
