# Rust create_ap supervision

`relayforge-service --native-create-ap --legacy-root /usr/lib/relayforge`
provides an opt-in Rust supervisor for the create_ap engine. It preserves
all ten desktop D-Bus methods, three signals, authorization and JSON formats.
The GTK settings, GNOME Shell extension and tray client need no changes. The
Python D-Bus/GLib worker is never constructed in this mode.

Rust also implements snapshots and child recovery through the
[native recovery runtime](native-recovery.md). A generated adapter of the pinned
Bash script configures hostapd, dnsmasq, interfaces and firewall rules while
calling Rust for all six recovery operations. Neither native service mode
requires a Python backend; the original compatibility mode remains available.

## Startup and ownership

Native configuration must select `BACKEND=create_ap`. Rust repeats live radio
preflight and resolves a permitted band/channel before launching anything. A
plan requiring an upstream band switch is rejected. COUNTRY overrides are also
rejected, so this mode does not deliberately change regulatory settings. Bridge
mode is declined because it reconfigures the upstream interface. It
never asks NetworkManager to reconnect the upstream. These checks cannot promise
that an unvalidated driver or the shell engine will preserve connectivity on
physical hardware; activation remains a separate hardware gate.

Rust freezes configuration into a random, mode-0600 file under the private
`/run/relayforge` directory. Credentials appear only in that private file, never
in argv, ownership JSON or returned helper diagnostics. Raw helper output is
drained with a size limit and discarded. Rust writes a synced intent journal,
launches a gated child in a separate process group, records its PID, boot ID and
kernel start time, then acknowledges execution through an anonymous pipe. A
parent crash before acknowledgement cannot launch the networking helper. Bash
runs in the foreground using create_ap's existing daemon acknowledgement path.

Readiness requires the exact PID acknowledgement, a live child with the expected
executable and arguments, one owned recovery snapshot, the recorded AP interface
identity and planned channel, and an unchanged upstream SSID/BSSID/frequency.
Readiness has a deadline. Client reports combine iw station observations with
leases from that instance's dnsmasq directory. Credentials and arbitrary helper
output are never forwarded through desktop status errors.

The native directory lifetime lock excludes concurrent native supervisors.
A pending journal from either native backend blocks switching service modes.
Stop must finish the previous backend's cleanup first. Existing compatibility
ownership markers also block production native startup; they are never adopted.

## Cancellation, monitoring and recovery

Stop cancels pending startup and waits for cleanup. Status/client methods remain
responsive while startup or cleanup runs. Signals target a pidfd only after
checking boot/start identity, user, executable and exact arguments. A reused PID
or changed command is never accepted. Graceful SIGUSR1 is followed by bounded
TERM/KILL escalation. A retained direct child also anchors process-group cleanup;
recovery after a supervisor crash never signals an unanchored process group.

Rust selects recovery directories only under create_ap's fixed private runtime
root and only when the snapshot has the exact random pidfile marker, owner PID,
start time and directory identity. It invokes `--recover` on those directories
and confirms their disappearance before removing the frozen configuration and
journal. Foreign hotspot snapshots remain untouched. Missing or changed owned
snapshots fail closed and retain evidence for a Stop retry; there is no global
orphan scan, `--stop` or adoption of another process's hotspot.

Monitoring checks process identity, AP channel and upstream identity. Unexpected
exit or drift triggers owned cleanup and reports failure. SIGTERM performs
cleanup; restart after supervisor death recovers the pending journal before
exposing desktop methods. Unconfirmed cleanup retains the journal and private
configuration and prevents a successful restart. Automatic resume and suspend
recovery are not implemented.

Band-switch requests return `success: false` without changing sharing. Selecting
another backend through SetConfig returns InvalidArgs in a fixed native mode.
PrepareFirewall returns true; create_ap performs its own networking rules during
startup. The Python supervisor's additional firewalld workaround is not ported.
The compatibility mode remains available for features beyond these boundaries.

## Staging without activation

Build and stage as an ordinary user:

```sh
cargo build --release --workspace --locked
python3 tools/stage-native.py --backend create_ap \
  --binary target/release/relayforge-service --output artifacts/create-ap-core
```

The fresh output directory contains seven payload files plus a hash/mode
manifest: the Rust binary, alternative systemd unit and D-Bus activation file,
original bus and Polkit policies, license and generated native Bash adapter. It
excludes the Python supervisor/worker, credentials, configuration and desktop
replacements. The source vendor files retain their original hashes; staging
applies only the reviewed helper-call transformation. Staging never installs
files, enables services or reloads D-Bus.

The systemd unit conflicts with both the compatibility service and native
NetworkManager unit, preserves the private runtime across restarts, and allows
bounded cleanup before shutdown. Installation still requires the backup,
configuration validation and controlled activation/rollback steps described in
[the native deployment guide](native-networkmanager.md). Do not remove a pending
journal to bypass failed ownership checks. No service was installed or activated
for this migration.

## Validation boundary

`--test-create-ap CAPTURE CONFIG STATE_DIR HELPER` is an internal ordinary-user
mode on a private session bus. A Bash simulator writes only into a temporary
directory; radio observations and resource snapshots are simulated. It exercises
real process creation, execution gating, pidfds, signals, cancellation, deadlines,
output bounds, recovery and the unmodified asynchronous desktop client.

Those checks do not establish live create_ap behavior, driver compatibility,
DHCP/DNS/NAT connectivity, real Polkit authorization, suspend/resume, installation
or rollback. The current Wi-Fi connection and installed service remain untouched.
