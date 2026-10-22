# Rust service migration

## Current boundaries

```text
unchanged GTK / GNOME Shell / tray controls
                  |
     original system D-Bus contract
                  |
       Rust service + Polkit checks
                  |
   Rust lifecycle and protocol validation
        |-- native configuration/store
        |-- native sysfs inventory
        |-- native radio parsing / startup preflight
        |-- opt-in native NetworkManager controller / ownership journal
        |-- opt-in Rust create_ap supervisor / gated Rust instance helper / recovery journal
        `-- anonymous stdin/stdout JSON pipes
                         |
              pinned Python backend adapter
                         |
             create_ap / NetworkManager / kernel
```

relayforge-core has no GLib, D-Bus, process or filesystem dependency. It validates
the complete merged configuration schema, models explicit sharing intent, preserves Stop
across delayed activation, rejects stale operation completions, and retains the
last observed active state when cleanup is unknown. SSID/interface status data
are separate from credential-bearing configuration. Native credential storage
and atomic persistence live in the Rust service. The core's Unicode-16 character
classification matches the pinned Python validator; persistence additionally
requires explicit stored credentials.

relayforge-service owns the existing D-Bus name without replacement or queueing.
It authorizes protected calls using the bus-assigned unique sender, passes that
identity to Polkit as a system-bus-name subject and fails closed on errors. Config
reads are protected because they contain credentials. Status and clients use
snapshots so backend work cannot block them. A bounded request budget reserves
capacity for Stop. Configuration is never placed in process arguments or emitted
in Rust diagnostics.

GetConfig, SetConfig, GetInterfaces and GetCapabilities are entirely native. Rust forbids these
requests at the worker boundary. Configuration writes use a transaction gate
shared with Start/band changes, and an exclusive configuration lock; Start and
Stop take shared configuration locks so Stop can cancel an in-flight startup.
Backend changes require both idle Rust state and a fresh legacy ownership/status
inspection in the backend operation queue. Status/client snapshots remain
responsive while configuration I/O runs on Tokio's blocking pool.

Native radio inspection uses bounded read-only iw commands. Fresh Start requests
pass native configuration/channel/concurrency checks before backend dispatch;
Stop cancels pending inspection. The backend still rechecks live state and owns
profile validation, activation and recovery. See [native radio](native-radio.md).

compat/worker.py loads the pinned engine without registering a public D-Bus
object. The parent is its sole client. The original GLib operation scheduler
continues to serialize real backend work, with urgent Stop and suspend handling.
Worker input/output is bounded, replies carry request IDs, and signals are
validated before rebroadcast. Earlier signals are acknowledged before later
operation replies to preserve stream order. EOF, malformed input and termination
signals request backend cleanup. A broken transport produces an explicit failed
state; it never proves that network resources have disappeared.

In compatibility mode both system-mode processes still run with privileges. Native
NetworkManager mode has a single privileged Rust service; it does not spawn the
Python worker. See [native mode](native-networkmanager.md) for lifecycle, journal,
compatibility boundaries and staging. The native path still uses external iw/ip
utilities and delegates shared networking to NetworkManager.

Compatibility mode retains Python, GLib, Bash, hostapd, dnsmasq and driver
dependencies. Its adapter still owns actual network operations, ownership
journals, recovery and automatic resume policy.

## Next extraction steps

1. Completed: merged configuration schema, atomic persistence, secure new-file
   credential creation, and differential validation. Existing factory-password
   rotation and legacy interface-repair installer hooks are not implemented;
   existing files are retained. See [native configuration](native-configuration.md).
2. Completed: native interface enumeration, radio capability parsing and initial
   regulatory preflight. The pinned compatibility engine remains unchanged;
   native mode does not import its developer probe.
3. Completed for native mode: ownership intent journals, interface identity
   checks, cancellation, crash cleanup and retry after uncertain cleanup. Native
   automatic resume and sleep recovery remain future work.
4. Completed as an opt-in mode: native Rust NetworkManager control using volatile
   profiles and client-bound activation. Both NM-owned child interfaces and
   Rust-controlled iw helpers are covered by simulated fault tests. These tests
   do not prove driver or NM extension compatibility on a physical machine.
5. Completed as an opt-in mode: Rust create_ap process supervision, readiness,
   client reports, cancellation and owned recovery. The Rust instance helper now
   owns options, publication, locking, markers, signals and housekeeping;
   snapshot/child recovery, firewall transactions,
   AP creation/setup/teardown, NetworkManager managed-state handling, forwarding,
   daemon rendering/launch/readiness and startup radio/driver policy are now Rust. See
   [create_ap supervision](native-create-ap.md) and [native recovery](native-recovery.md).
   Compatibility mode remains until
   feature parity is demonstrated. See [firewall extraction](native-firewall.md);
   daemon launch/configuration now use the [Rust runtime](native-daemons.md).
6. A native service, bus activation and policy payload can now be staged for
   review. Distribution packages, deployed upgrade/rollback validation and
   additional service hardening remain pending. Physical DHCP, DNS, verified
   HTTPS, cancellation, crashes, radio toggles, suspend and multiple-driver
   activation checks remain deployment gates.

The milestone tests establish protocol/client and simulated lifecycle behavior.
They do not transfer Wi-Fi Relay's release certification to RelayForge. The
[physical radio report](physical-radio-validation.md) covers read-only adapter
inspection. No native hotspot, real Polkit authentication agent, GNOME Shell
load, firewall mutation or native AP interface creation has been physically
validated. The laptop's existing Wi-Fi remains untouched by native activation.

## Reference documentation

- [zbus 4.4 API](https://docs.rs/zbus/4.4.0/zbus/): chosen for the available
  dependency baseline; Cargo.lock pins the resolved dependency tree.
- [NetworkManager D-Bus API](https://networkmanager.dev/docs/api/latest/gdbus-org.freedesktop.NetworkManager.html):
  AddAndActivateConnection2 supports volatile persistence and dbus-client lifetime.
- [Polkit Authority interface](https://www.freedesktop.org/software/polkit/docs/latest/eggdbus-interface-org.freedesktop.PolicyKit1.Authority.html):
  CheckAuthorization and system-bus-name subjects.

Native create_ap [network transactions](native-network-state.md) now own AP
link/type/address setup, teardown and shared forwarding sysctl restoration.
[Rust device ownership](native-device-ownership.md) now handles interface creation
and NetworkManager managed-state restoration. [Rust daemon supervision](native-daemons.md)
now handles configuration, launch, readiness and private leases.
