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
and durable recovery from interrupted final cleanup. Its
[validation report](docs/backend-audit-validation.md) records the exact candidate
and offline results. [Native suspend/resume](docs/native-suspend.md) now coordinates
logind sleep, owned cleanup and recovery of a prior authorized sharing request.
[Rust band switching](docs/native-band-switch.md) now reconnects the upstream to a
permitted hotspot channel over D-Bus, and a [Wi-Fi radio watch](docs/native-radio-watch.md)
releases owned sharing when the radio is switched off.
Its [validation report](docs/native-suspend-validation.md) records offline coverage
and remaining live-suspend limits. A [race audit](docs/native-suspend-audit.md)
hardens cancellation, status publication and inhibitor recovery; its
[validation report](docs/native-suspend-audit-validation.md) records the tested revision.
[Native offline deployment](docs/native-deployment.md) now supports installation,
upgrade, interrupted recovery and rollback in an explicitly selected dormant image.
Its [validation report](docs/native-deployment-validation.md) records the tested
revision and the remaining privileged/live-deployment gates.
The [deployment race audit](docs/native-deployment-audit-validation.md) hardens
interrupted aborts, runtime exclusion, recovery syncing and rollback preflight.
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
cargo build --release --locked -p relayforge-service
cargo test --workspace --locked
python3 tools/check-vendor.py
dbus-run-session -- /usr/bin/python3 -m unittest discover -s tests -v
```

Tests use a disposable session bus and simulated networking. They do not install
packages, start a hotspot, change the firewall, or use a physical Wi-Fi adapter.
The deployment suites stage the service executable as a payload file, and the
deployment engine refuses any single file larger than 64 MiB; a development build
with full symbols exceeds that bound, so the suite uses an up-to-date optimized
build when one exists. Set `RELAYFORGE_BINARY` to choose an executable
explicitly. Checked-out fixtures must not be group- or world-writable, because
the service rejects insecure configuration and installation trees
(`umask 0022; chmod -R g-w,o-w .` repairs a permissive checkout).

## Build a Debian package

On the target Debian/Ubuntu distribution and architecture, install the build
prerequisites: Rust 1.97+, Python 3.11+, and `dpkg-dev`. Then run as an ordinary
user from the repository root:

```sh
./tools/build-deb.sh
# Alternative native backend:
./tools/build-deb.sh --backend create_ap --version 0.1.1~preview1
```

The script builds the locked release executable and writes a `.deb` under
`artifacts/deb/`. Use `--output-dir PATH` to change the destination, or
`--binary /absolute/path/relayforge-service` to package an existing release ELF
without invoking Cargo. An existing package of the same name is preserved unless
`--force` is passed; a forced replacement is written to a temporary file and renamed
into place, so an interrupted build never leaves a partial package. Each package
contains one selected backend, the GTK settings/tray code, settings and tray
launchers, a login autostart entry, D-Bus/Polkit policies and its systemd unit.
GTK3 and Ayatana AppIndicator support are required package dependencies.
The package ships its own Polkit action
(`packaging/io.github.erhanzeyrek.WifiHotspot.policy`): the active local session is
authorized without a password, so opening settings, saving and toggling the hotspot
never prompt. Remote and inactive sessions are still denied. The policy still gates
every protected call, and the pinned upstream policy that asks for administrator
authentication on each call is available in `vendor/wifi-relay/data/`.
The Wi-Fi Relay GNOME Shell extension ships in the package and is enabled on
first login; patched NetworkManager applet, NetworkManager core and
gnome-control-center integrations are not included. Shared-library dependencies are calculated
with `dpkg-shlibdeps`; generate packages on the target distribution rather than
assuming a binary built on a newer distribution will work on an older one.

Installation does not generate credentials, enable or start a service. In a
prepared test environment, install the package with `sudo apt install ./PATH.deb`.
For a fresh NetworkManager installation, choose the real Wi-Fi interface and run:

```sh
sudo /usr/lib/relayforge/relayforge-service --init-config /etc/wifi-hotspot.conf \
  --wifi-interface wlp2s0 --backend networkmanager
sudo systemctl daemon-reload
sudo systemctl start relayforge-native.service
wifi-hotspot-settings
```

### Troubleshooting: "Hotspot service unavailable"

The settings window shows this whenever `io.github.erhanzeyrek.WifiHotspot` has no
owner on the system bus. The most common cause on a fresh installation is a missing
`/etc/wifi-hotspot.conf`: the service starts, finds nothing to read, and exits with
status 17. Check the unit's journal for the exact path and the command to run:

```sh
journalctl -u relayforge-native.service -n 20
sudo /usr/lib/relayforge/relayforge-service --init-config /etc/wifi-hotspot.conf \
  --wifi-interface wlp2s0 --backend networkmanager
sudo systemctl start relayforge-native.service
```

Status 17 is deliberately listed in `RestartPreventExitStatus=` on both units and
`StartLimitIntervalSec=300` bounds retries, so an unconfigured install reports the
problem once instead of restarting every few seconds. Initialization never replaces
an existing configuration, so it is safe to run when one may already exist.

The packaged tray is maintained in `desktop/tray.py`, adapted from the pinned
Wi-Fi Relay source. It keeps service failure reasons visible in the menu, offers
channel guidance when concurrent sharing is blocked, and explains unavailable
controls. The imported vendor snapshot remains unchanged.

Desktop controls start automatically at your next graphical login, honoring the
saved `Start desktop controls at login` preference and the icon visibility
preference. The login helper picks the integration for your session:

- **GNOME** — the bundled Wi-Fi Relay Shell extension (top-bar indicator and a
  Quick Settings toggle) is enabled once at first login, unless you already
  disabled it in the Extensions app. Manage it like any other extension.
- **KDE Plasma** — the StatusNotifier tray works with the panel's built-in
  host; no extra packages are needed.
- **XFCE** — the panel shows the tray through `xfce4-statusnotifier-plugin`,
  which the package recommends. Install and add it to the panel if the icon
  does not appear.
- **Other desktops** — any panel with StatusNotifier/AppIndicator support can
  display the tray.

To show the tray icon now, run `wifi-hotspot-tray` as your desktop user
(without sudo), or open **Wi-Fi Relay Tray** from the application menu. All
controls talk to the same D-Bus service, which shares the upstream through
NetworkManager (or the create_ap backend). You can disable package autostart per
user by creating `~/.config/autostart/relayforge-tray.desktop` containing:

```ini
[Desktop Entry]
Type=Application
Name=Wi-Fi Relay Tray
Hidden=true
```

For a `create_ap` package, initialize with `--backend create_ap` and start
`relayforge-create-ap.service`. Existing configuration is preserved; its backend
must match the selected service. Stop the old service and finish owned network
cleanup before upgrading or switching backends, then reload systemd and start the
selected service manually. Remove/reconfigure conflicting Wi-Fi Relay packages
before installation; their D-Bus identity and desktop paths overlap. The native
hardware-validation limits described below still apply to these packages.

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
existing path. It does not detect the build machine's adapters unless
`--detect-interface` is passed, and then only when exactly one Wi-Fi adapter
exists. The optional SSID/interface arguments set fresh-install values only; no
password is printed.
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

An upgrade from the packed Wi-Fi Relay default can leave the literal `wlan0`
assignment behind on hardware whose adapter has another name. The ported
installer repair rewrites only those obsolete assignments, keeps every other
line, comment and credential byte-for-byte, and never writes when `wlan0` is
present or the hardware is ambiguous:

```sh
sudo ./target/release/relayforge-service --repair-config-interface /etc/wifi-hotspot.conf
```

Pass `--wifi-interface NAME` to choose the replacement on a machine with several
Wi-Fi adapters.

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

The Rust [offline deployment engine](docs/native-deployment.md) can apply and roll
back a staged payload in a dormant image. It refuses the live root and never enables
or starts services. The Debian build script creates a local package; no
distribution package release or automatic live service handover is supplied.
Staging itself only creates a reviewable payload. Native hotspot activation
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
