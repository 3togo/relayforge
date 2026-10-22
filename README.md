# RelayForge

A Rust core for Linux Wi-Fi sharing, with the existing Wi-Fi Relay GTK settings,
GNOME Shell extension and tray controls. The name is a working suggestion, not a
claim of name or trademark availability.

This is an early migration project. Rust owns the public D-Bus service,
authorization, the complete configuration schema and credential store, Linux
interface inventory, radio capability inspection, startup preflight, lifecycle
intent and status snapshots. A private Python
subprocess retains the pinned Wi-Fi Relay networking
implementation, including create_ap and the experimental NetworkManager backend.
This is not yet an entirely Rust networking stack or a production replacement.

## Build and check

Requires Rust 1.97+, Cargo, Python 3 with PyGObject/Gio, and dbus-run-session.
Dependency versions are pinned in Cargo.lock. The checked build environment and
results are recorded in [the initial validation](docs/validation.md) and
[native configuration migration](docs/native-configuration.md).
The [native radio migration](docs/native-radio.md) describes capability parsing
and startup preflight. Its [validation report](docs/native-radio-validation.md)
records the exact implementation revision and remote checks.

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

## Compatibility and deployment boundary

The service preserves `io.github.erhanzeyrek.WifiHotspot`, its object path, all ten
methods, three signals, and JSON/string/boolean reply formats. Keeping these
identifiers allows the existing desktop controls to work unchanged. RelayForge
and Wi-Fi Relay cannot own that service name simultaneously; RelayForge refuses
replacement and queueing.

System mode is explicit:

```text
relayforge-service --system --legacy-root /usr/lib/relayforge
```

The installation root must contain `compat/worker.py` and `vendor/wifi-relay/`.
It and every parent/payload file must be root-owned and not group/world writable;
symlinks are rejected. A root-owned, single-link regular `/etc/wifi-hotspot.conf`
with mode 0600 and an explicit PASSPHRASE is required. There is no factory-password or user-config fallback in system
mode. Existing D-Bus and Polkit policies must be installed for the compatibility
name. System mode always uses Polkit; demo authorization cannot enable it.

No installer, automatic service replacement, system activation change, package
release, or live networking trial is supplied by this development project. Run only
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
