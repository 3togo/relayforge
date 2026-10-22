# Native NetworkManager service mode

`relayforge-service --native-networkmanager` provides a Rust-controlled
NetworkManager sharing path through the existing desktop D-Bus service. It never
constructs the Python worker and does not invoke nmcli. The existing compatibility
mode remains available for create_ap and the pinned Python engine. Native mode is
opt-in; no installed service or network is changed by building or staging it.

## Activation and ownership

Start first validates the private native configuration and current radio
permissions. A second live inspection checks the connected managed upstream,
its NetworkManager profile UUID, SSID, BSSID, frequency, kernel interface index
and MAC. It also checks AP capability, PHY siblings and connected IPv4 routes.
An AP subnet overlapping an existing connected network is rejected. The profile
is pinned to the existing upstream band/channel, never becomes a default route,
and uses WPA2/CCMP, the requested SSID/hidden/isolation settings and shared IPv4
with the configured gateway. NetworkManager owns DHCP, DNS and NAT routing.

Each session uses a private D-Bus connection pinned to NetworkManager's unique
owner. Activation calls AddAndActivateConnection2 with `persist=volatile` and
`bind-activation=dbus-client`. Credentials are in the D-Bus settings payload,
with the PSK marked not-saved; they are never in argv, diagnostics, ownership
records or staged configuration. A random UUID and private ownership token mark
the exact profile. The existing upstream profile is never edited or deactivated.

Two interface paths are supported:

- If NetworkManager advertises the pinned downstream AP+STA capability `0x7001`,
  its child-interface extension is requested through the same parent user-data
  key as the reference backend. This is not a standard upstream NM capability.
- Otherwise Rust invokes bounded iw commands to create its own randomly named
  AP interface with a locally administered MAC. It waits for NM discovery and
  sets Managed/Autoconnect only on that child. If necessary it restores AP mode
  on that same verified child before activation.

The service writes a credential-free intent record before creating resources.
The record is updated with the observed interface index; native-child identity
is captured after activation. `/run/relayforge` must be service-owned mode 0700;
its lifetime lock excludes a second journal user. Atomic, synced journal writes
use fd-relative no-follow operations, mode-0600 single-link files, bounded input,
validated fields and checks against external edits. No journal value is used as
an arbitrary command or path.

Stop cancels startup through the service revision watch and waits for cleanup.
The private activation connection is closed even if the activation reply was
lost. Recovery checks the exact profile UUID, profile ID, interface name, AP mode
and ownership token before deleting a profile. Helper interfaces additionally
need the recorded PHY/MAC/index before removal. Native children are left to
NetworkManager for deletion. Cleanup verifies absence of both resources before
clearing the journal; uncertain cleanup returns failure and retains the record.
Start cannot turn a failed cleanup into a successful idempotent activation.

Monitoring verifies the upstream, active connection and AP channel, then reports
authorized station MACs with observed IPv4 neighbor addresses. Hostnames are
currently empty. Status and clients remain Rust snapshots while backend work
runs. Upstream changes or a sharing disconnect trigger owned-resource cleanup;
there is no automatic reconnection or automatic reactivation. SIGTERM requests
cleanup. Following process death, the next service startup recovers its journal
before exposing the desktop methods. A stale compatibility ownership record
blocks native startup rather than being adopted or deleted.

## Desktop and mode boundaries

The bus name, object path, ten methods, three signals, Polkit subject and JSON
shapes remain unchanged. The unmodified asynchronous desktop client is exercised
against this backend, including status/client signals.

Native mode requires `BACKEND=networkmanager`. Other configuration fields use
the existing native schema and the reference NetworkManager option restrictions.
Selecting create_ap in this mode returns an explicit InvalidArgs response; use
the compatibility service mode for that backend. There is no live migration
between service implementations. Band-switch requests return `success: false`
without changing the current sharing state or upstream. PrepareFirewall returns
true because NM performs its shared-connection firewall work during activation;
there is no additional firewalld workaround in this path.

Read-only diagnostics and demo modes are retained. The internal
`--test-networkmanager CAPTURE CONFIG STATE_DIR` mode uses only a private session
bus, replayed initial radio data and the fake manager's simulated kernel API.
It cannot execute system iw mutations or contact the system bus.

## Staging and future installation

Build and stage as an ordinary user:

```sh
cargo build --release --workspace --locked
python3 tools/stage-native.py --binary target/release/relayforge-service \
  --output artifacts/native-core
```

The output directory must not exist. It contains the executable, systemd unit,
D-Bus activation file, original bus/Polkit policies, MIT license and a hash/mode
manifest. It contains no credentials or configuration, no Python backend and no
desktop-control replacement. Staging never installs files, enables a unit or
reloads D-Bus. The output is a reviewable payload, not a distribution package.

For a future fresh installation, configuration initialization supports:

```text
relayforge-service --init-config /etc/wifi-hotspot.conf --backend networkmanager \
  --wifi-interface wlp2s0 --ssid "My hotspot"
```

Initialization creates a random private password; an existing configuration is
left byte-for-byte unchanged, including its backend selection. A migration must
explicitly validate the existing BACKEND setting before starting native mode.
The unit requires NetworkManager, conflicts with the old service, and preserves
its runtime directory across process restarts. A failed cleanup remains visible
through a retained journal and failed Stop result.

Installation/rollback preparation must preserve the existing configuration and
all replaced service, bus-activation and policy files, including ownership and
permissions. Stop and verify the old hotspot before replacing its service. Apply
only the reviewed payload with root ownership, then validate configuration,
unit activation and desktop authorization in a controlled hardware window.
Rollback first stops native sharing and verifies cleanup; restore the previous
files and original backend setting before re-enabling the old service. Do not
force-remove a journal whose identity checks fail. No deployment or rollback was
performed during this implementation.

## Validation boundary

The fake manager validates real D-Bus signatures/profile contents and simulates
both interface paths, AP-mode restoration, partial creation, denied/failed
activation, reply timeouts, cancellation, changed upstream, occupied PHYs, subnet
collisions, client signals, process death, manager restart, reused interface
identities, modified profile ownership, shutdown and cleanup retry. Journal and
staging tests cover filesystem boundaries. The test suite does not prove real
NetworkManager activation, driver behavior, DHCP/DNS/NAT connectivity, sleep/resume
or distro installation. Physical activation remains deferred to preserve the
current Wi-Fi connection. Automatic resume and seamless backend-mode switching
remain future work.

References: [NM activation API](https://networkmanager.dev/docs/api/latest/gdbus-org.freedesktop.NetworkManager.html),
[NM connection settings](https://networkmanager.dev/docs/api/latest/settings-connection.html),
[wireless settings](https://networkmanager.dev/docs/api/latest/settings-802-11-wireless.html),
[device API](https://networkmanager.dev/docs/api/latest/gdbus-org.freedesktop.NetworkManager.Device.html).
