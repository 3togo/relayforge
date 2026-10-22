# Native create_ap recovery runtime

The native create_ap payload now uses Rust for snapshots and child recovery as
well as supervision. It contains no Python backend or recovery helper. The
existing GTK, GNOME Shell and tray controls keep the same D-Bus contract; the
optional original compatibility service and its pinned Python files are retained
in source. [Rust daemon supervision](native-daemons.md) now configures/launches
hostapd/dnsmasq; interface creation and
NetworkManager managed-state handling use [Rust device ownership](native-device-ownership.md). Rust owns
[firewall transactions](native-firewall.md) and
[AP setup/forwarding transactions](native-network-state.md). No networking engine or desktop component was installed or activated
for this migration.

## Bash boundary and provenance

`tools/stage-native.py --backend create_ap` generates `native/create_ap` from the
pinned vendor script. The transformation changes the helper location and exactly
six Python recovery calls, direct firewall commands, AP setup/teardown and
forwarding sysctl operations into calls to
the staged Rust executable's internal
`--create-ap-runtime` entry point. It verifies the source hash against the vendor manifest and refuses an
unexpected protocol shape or any remaining Python recovery reference. The remaining Bash networking logic and original notices remain. The 48-file vendor snapshot stays byte-for-byte unchanged.

The resulting seven-file payload includes the Rust service, generated Bash
adapter, systemd unit, bus activation, original bus/Polkit policies and license.
There is no `.py` file, configuration, credential or desktop replacement in it.
Python remains a build/test dependency for staging, reference parity and desktop
integration tests; it is not used by native backend recovery.

The Rust actions are `save`, `load`, `record-child`, `stop-children`,
`check-interfaces` and `verify-cleanup`, plus `firewall-add`, `firewall-remove` and
`firewall-check`, `interface-configure`, `interface-remove`, `forwarding-enable`,
`forwarding-release`, `network-check`, `interface-create`, `device-fields`,
`device-release` and `device-restore`. Daemon actions are `daemons-configure`,
`daemons-run`, `daemon-exec` and `daemons-leases`. They run before D-Bus registration.
Production actions require root and accept only instance/common directories in
the fixed private engine runtime. There is no generic subprocess executor or
unrestricted recovery-directory override. The supervisor still selects exact
owned instances and retains its credential-free journal until cleanup succeeds.

## Snapshot and process safeguards

Rust reads the existing JSON shape: owner PID/start time/executable/argv SHA-256,
allowlisted shell fields and arrays, and recorded interface indexes/MACs. This
supports records written by the preceding native supervisor's Python helper when
used with its matching Bash installation. Rust adds strict types, required
fields, native pidfile-marker validation, bounded values and semantic checks
before returning shell assignments. Values are always quoted, including array
elements. Credentials are not accepted as snapshot fields or echoed in errors.

Snapshots use mode-0600 single-link regular files, fd-relative no-follow reads,
bounded nonblocking input and atomic temporary-file/rename commits with file and
directory fsync. Unsafe directories, links, external edits, oversized files and
unknown fields fail closed. Save checks existing interface identities before
updating observed values, so a reused netdev is never silently adopted.

Child identities retain the pinned SHA-256 representation. Signals use pidfds
and recheck process birth, executable, arguments and the freshly observed UID.
Root may record a verified dnsmasq after its normal privilege drop; initial
hostapd/haveged/watchdog recording still requires the service UID. A changed command or
reused PID is not signalled. Verified children receive TERM followed by bounded
KILL escalation. The daemonization-before-recording window can be recovered only
for a known hostapd/dnsmasq/haveged executable using this instance's private path.
A bare watchdog PID cannot be adopted; initial watchdog recording additionally
requires the direct live owner as parent. Malformed child records retain evidence
and prevent successful cleanup.

Load refuses a live owner, stops verified children, then validates interfaces
before allowing Bash recovery assignments. This stops verified broadcasters even
when a later interface-identity mismatch prevents changing networking. Existing
ifindexes must match; owned virtual/bridge interfaces also require their recorded
MAC. An interface created after the planned snapshot is accepted only for a
virtual AP with the previously recorded MAC. An unrecorded bridge is refused.

Cleanup verification requires no owned live children, no surviving owned virtual
interfaces/bridges, and no instance-commented rules in either iptables table.
Read-only firewall queries have bounded output and execution deadlines; failed
queries cannot prove cleanup. Unknown or unverified child state also blocks
success. No global orphan scan or foreign hotspot adoption was added.

## Upgrade and operational limits

The native service now expects `ROOT/native/create_ap`, not the original
`ROOT/vendor/wifi-relay/daemon/create_ap`. Its ownership journal deliberately
refuses a different helper installation. Before changing payload layouts, use
the previous matching executable/helper to Stop and confirm cleanup. A pending
old journal must not be deleted or edited to bypass that check. Keep the previous
reviewed payload and configuration backup available for rollback. No live
upgrade, installation or rollback was performed here.

Native mode still declines upstream band switching, bridge mode and COUNTRY
overrides. Automatic resume, suspend recovery and the old supervisor's additional
firewalld workaround remain unimplemented. Physical activation, driver behavior,
DHCP/DNS/NAT connectivity and deployment remain separate hardware gates.

## Isolated tests

`--test-ap-runtime ROOT ACTION PATH ...` is an internal ordinary-user entry point.
It confines files to private temporary runtime directories, reads simulated
interface/firewall data and never invokes system firewall tools. Real process
identity/pidfd behavior is tested with disposable Rust child executables. A Bash
simulator also connects the supervisor and runtime through the unchanged desktop
client on a private bus. The actual generated Bash networking path is not started.
See [create_ap supervision](native-create-ap.md) for staging and service behavior.
