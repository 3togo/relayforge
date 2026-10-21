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
           /                    \
 native configuration        native sysfs inventory
                  |
      anonymous stdin/stdout JSON pipes
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

GetConfig, SetConfig and GetInterfaces are entirely native. Rust forbids these
requests at the worker boundary. Configuration writes use a transaction gate
shared with Start/band changes, and an exclusive configuration lock; Start and
Stop take shared configuration locks so Stop can cancel an in-flight startup.
Backend changes require both idle Rust state and a fresh legacy ownership/status
inspection in the backend operation queue. Status/client snapshots remain
responsive while configuration I/O runs on Tokio's blocking pool.

compat/worker.py loads the pinned engine without registering a public D-Bus
object. The parent is its sole client. The original GLib operation scheduler
continues to serialize real backend work, with urgent Stop and suspend handling.
Worker input/output is bounded, replies carry request IDs, and signals are
validated before rebroadcast. Earlier signals are acknowledged before later
operation replies to preserve stream order. EOF, malformed input and termination
signals request backend cleanup. A broken transport produces an explicit failed
state; it never proves that network resources have disappeared.

Both system-mode processes still run with privileges. This step does not reduce
the entire networking implementation's trusted code base or remove Python,
GLib, Bash, hostapd, dnsmasq or driver dependencies. The adapter still owns actual
network operations, ownership journals, recovery and automatic resume policy.

## Next extraction steps

1. Completed: merged configuration schema, atomic persistence, secure new-file
   credential creation, and differential validation. Existing factory-password
   rotation and legacy interface-repair installer hooks are not implemented;
   existing files are retained. See [native configuration](native-configuration.md).
2. Completed: native interface enumeration. Next: radio capabilities and
   regulatory preflight in a backend-independent
   capability module. Remove the production-backend-to-developer-probe import
   relationship. Do not relax channel restrictions to make a test pass.
3. Port ownership journals, PID/interface identity checks, timeout/cancellation,
   crash cleanup and sleep recovery. Use deterministic fault tests and replay
   the historical failure cases before permitting privileged deployment.
4. Implement a native Rust NetworkManager adapter. Retain volatile profiles and
   client-bound activation; retain conservative AP+STA preflight. The documented
   AddAndActivateConnection2 options do not establish native virtual-interface
   ownership or radio compatibility on a particular machine.
5. Move create_ap supervision into Rust while retaining the current compatibility
   path until feature parity is demonstrated. Replacing shell network management
   itself is a separate project decision.
6. Add reviewed packages, explicit conflict/upgrade/rollback behavior and system
   service hardening. Then replay physical DHCP, DNS, verified HTTPS, cancellation,
   daemon/worker crash, radio toggles, suspend and multiple-driver tests.

The milestone tests establish protocol/client and simulated lifecycle behavior.
They do not transfer Wi-Fi Relay's release certification to RelayForge. No live
hotspot, real Polkit authentication agent, physical desktop, GNOME Shell load,
firewall rule or kernel interface was exercised for this project.

## Reference documentation

- [zbus 4.4 API](https://docs.rs/zbus/4.4.0/zbus/): chosen for the available
  dependency baseline; Cargo.lock pins the resolved dependency tree.
- [NetworkManager D-Bus API](https://networkmanager.dev/docs/api/latest/gdbus-org.freedesktop.NetworkManager.html):
  AddAndActivateConnection2 supports volatile persistence and dbus-client lifetime.
- [Polkit Authority interface](https://www.freedesktop.org/software/polkit/docs/latest/eggdbus-interface-org.freedesktop.PolicyKit1.Authority.html):
  CheckAuthorization and system-bus-name subjects.
