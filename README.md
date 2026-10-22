# RelayForge

A Rust core for Linux Wi-Fi sharing, with the existing Wi-Fi Relay GTK settings,
GNOME Shell extension and tray controls. The name is a working suggestion, not a
claim of name or trademark availability.

This is an early migration project. Rust owns the desktop D-Bus service,
authorization, configuration and credential storage, interface/radio inspection,
lifecycle intent and status snapshots. The opt-in
[native NetworkManager mode](docs/native-networkmanager.md) also controls
activation, clients, cancellation, ownership recovery and cleanup without a
Python worker. The opt-in [Rust create_ap supervisor](docs/native-create-ap.md)
owns helper processes, readiness, clients and recovery. Its
[native recovery runtime](docs/native-recovery.md) replaces the remaining Python
helper. [Rust firewall transactions](docs/native-firewall.md) now own NAT/DNS/DHCP
rules. [Rust network transactions](docs/native-network-state.md) now own AP setup,
teardown and forwarding leases. [Rust device ownership](docs/native-device-ownership.md)
now handles interface creation and NetworkManager release/restoration. The native
[daemon runtime](docs/native-daemons.md) now renders and launches hostapd/dnsmasq,
with gated child recovery and private leases. [Rust startup radio checks](docs/native-startup-radio.md)
now enforce the frozen plan and driver policy through launch. The
[Rust instance runner](docs/native-instance.md) owns options, instance markers,
signals and housekeeping; native staging includes an ELF helper instead of Bash.
The [backend audit](docs/backend-audit.md) adds connected-subnet collision checks
and durable recovery from interrupted final cleanup.
The compatibility mode retains the original Python engine. These native modes
are not yet validated production replacements.

## Build and check

Build/tests require Rust 1.97+, Cargo, Python 3 with PyGObject/Gio, and dbus-run-session.
The native NetworkManager runtime uses NetworkManager, iw and ip without Python.
Native create_ap uses iw/ip, hostapd, dnsmasq and iptables; its new backend
requires neither Bash nor Python. Historical Bash helpers can still be supervised
and recovered during a controlled migration.
Dependency versions are pinned in Cargo.lock. The checked build environment and
results are recorded in [the initial validation](docs/validation.md) and
[native configuration migration](docs/native-configuration.md).
The [native radio migration](docs/native-radio.md) describes capability parsing
and startup preflight. Its [validation report](docs/native-radio-validation.md)
records the exact implementation revision and remote checks. The subsequent
[physical radio report](docs/physical-radio-validation.md) records live read-only
checks and the constraint preventing hotspot activation without changing upstream.

```sh
cargo build --workspace --all-targets --locked
cargo test --workspace --locked
python3 tools/check-vendor.py
dbus-run-session -- /usr/bin/python3 -m unittest discover -s tests -v
```

Tests use a disposable session bus and simulated networking. They do not install
packages, start a hotspot, change the firewall, or use a physical Wi-Fi adapter.

## Try the reused desktop controls

On a machine with GTK4/libadwaita and a graphical session:

```sh
dbus-run-session -- sh -c '
  export DBUS_SYSTEM_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS"
  ./target/debug/relayforge-service --demo &
  service_pid=$!
  trap "kill $service_pid; wait $service_pid" EXIT
  /usr/bin/python3 vendor/wifi-relay/settings/launcher.py --standalone
'
```

Demo mode uses only in-memory settings and fictitious adapters; it never spawns
the compatibility worker. The reused controls still display their Wi-Fi Relay
branding. `--demo-deny` tests denied requests; `--demo-polkit` tests the real
Polkit call path against an authority on the disposable bus. Neither connects to
the system bus. `--demo` and `--demo-config` authorize callers with the same UID only.

To test native persistence without networking, create a new private configuration
in an existing directory owned by your user, then start a demo using that file:

```sh
./target/debug/relayforge-service --init-config /absolute/private/directory/hotspot.conf \
  --ssid "My hotspot" --wifi-interface wlp2s0
dbus-run-session -- ./target/debug/relayforge-service \
  --demo-config /absolute/private/directory/hotspot.conf
```

Initialization creates a random password, writes mode 0600 and never replaces an
existing path. It does not detect the build machine's adapters. The optional
SSID/interface arguments set fresh-install values only; no password is printed.
`--demo-config` uses the same native disk store as system mode and simulated
networking. GTK controls can connect with the private-bus environment from the
previous example.

Read-only checks against a physical adapter are also available:

```sh
./target/release/relayforge-service --inspect-radio wlp2s0
./target/release/relayforge-service --preflight-radio /absolute/private/directory/hotspot.conf
```

These commands exit without opening D-Bus or starting the networking worker.
Preflight reports a plan; it does not activate sharing or switch the upstream
connection. See [native radio diagnostics](docs/native-radio.md) for limitations.

## Compatibility and deployment boundary

The service preserves `io.github.erhanzeyrek.WifiHotspot`, its object path, all ten
methods, three signals, and JSON/string/boolean reply formats. Keeping these
identifiers allows the existing desktop controls to work unchanged. RelayForge
and Wi-Fi Relay cannot own that service name simultaneously; RelayForge refuses
replacement and queueing.

Compatibility system mode is explicit:

```text
relayforge-service --system --legacy-root /usr/lib/relayforge
```

The installation root must contain `compat/worker.py` and `vendor/wifi-relay/`.
It and every parent/payload file must be root-owned and not group/world writable;
symlinks are rejected. A root-owned, single-link regular `/etc/wifi-hotspot.conf`
with mode 0600 and an explicit PASSPHRASE is required. There is no factory-password or user-config fallback in system
mode. Existing D-Bus and Polkit policies must be installed for the compatibility
name. System mode always uses Polkit; demo authorization cannot enable it.

The separate native service uses `--native-networkmanager` and requires
`BACKEND=networkmanager` in the same private configuration. It keeps the desktop
wire contract and declines band switching to preserve upstream. See the
[native mode and staging guide](docs/native-networkmanager.md) and
[validation report](docs/native-networkmanager-validation.md). No native service
has been installed or enabled by this migration.

The alternative `--native-create-ap --legacy-root /usr/lib/relayforge` mode requires
`BACKEND=create_ap`. See [Rust create_ap supervision](docs/native-create-ap.md) for
its ownership rules, compatibility limits and `--backend create_ap` staging option.
The [create_ap validation report](docs/native-create-ap-validation.md) records the
exact candidate, optimized executable and remote checks. The subsequent
[native recovery validation](docs/native-recovery-validation.md) records removal
of the Python helper from the native payload and its process/filesystem checks.
The [firewall extraction report](docs/native-firewall-validation.md) records the
subsequent NAT/DNS/DHCP rule migration and failure-injection checks. The
[AP setup and forwarding report](docs/native-network-state-validation.md) records
the next Rust extraction and its interrupted-transaction and desktop checks. The
[device ownership report](docs/native-device-ownership-validation.md) records
interface creation and NetworkManager restoration, including private-bus checks.
The [daemon runtime report](docs/native-daemons-validation.md) records native
configuration, gated launch, readiness, private leases and desktop failure checks.
The [startup radio report](docs/native-startup-radio-validation.md) records the
subsequent driver/channel extraction, baseline checks and desktop rejection test.
The [instance runner report](docs/native-instance-validation.md) records native
option parsing, signal cleanup, directory ownership, housekeeping and ELF staging.

No installer or package release is supplied. Staging creates a reviewable payload;
it does not replace services or change system activation. Native hotspot activation
has not been tested on physical hardware. Run only
in a prepared disposable environment until the migration and hardware gates in
[the architecture plan](docs/architecture.md) are completed. The original
checkout, installed services and networking configuration are independent of this
source project.

## Source and licenses

`vendor/wifi-relay` is a source-only snapshot from
[3togo/gnome-wifi-hotspot](https://github.com/3togo/gnome-wifi-hotspot), revision
`12f9f78e2cf4e9f41abacf94fbf5f5524bf47814`. It retains original file contents,
artwork, notices and identities. [vendor/manifest.json](vendor/manifest.json)
records every reused file hash. No original Git history, device logs, machine
configuration, signing material or integration patches were copied.

New Rust and adapter code is MIT-licensed. The original MIT notice is retained
in LICENSE. create_ap retains its BSD-2-Clause notices; the QR encoder retains
Project Nayuki's MIT notice; the copied AppStream metadata retains its CC0 notice.
See [THIRD_PARTY.md](THIRD_PARTY.md).
