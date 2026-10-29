# RelayForge 0.1.0 (2026-10-29)

Initial native release of RelayForge, a Linux Wi-Fi sharing tool that turns a
wireless adapter into a hotspot while keeping the upstream Wi-Fi connection live.

## What ships

- **Native Rust service** (`relayforge-service`) on the system D-Bus interface
  `io.github.erhanzeyrek.WifiHotspot`, with two selectable backends:
  - `networkmanager` (default) — drives NetworkManager directly and owns a
    dedicated AP interface on the same physical device as the upstream STA.
  - `create_ap` — wraps the pinned `create_ap` helper with native ownership,
    recovery and firewall controls.
- **GTK 4 settings window** (`wifi-hotspot-settings`) for configuration,
  start/stop, live status, connected clients and a connection QR code.
- **Desktop status indicator** (`wifi-hotspot-tray`) via Ayatana AppIndicator
  (KDE, XFCE and compatible environments).
- **GNOME Shell extension** (`wifi-relay@3togo.github.io`) enabled by the
  first-login dispatcher (`wifi-hotspot-enable-extension`).
- **Polkit split**: `manage` actions are `allow_active=yes`; credential reads
  use `auth_self_keep`.
- **Ownership, recovery and firewall** semantics retained from the pinned
  upstream daemon; a missing/invalid configuration exits with status 17 so
  systemd does not restart it.
- Debian packaging (`tools/build-deb.py`): stripped ELF, gzipped changelog and
  man pages, `Recommends: xfce4-statusnotifier-plugin`. Both backends build
  lintian-clean.

## Validation

- `cargo test --workspace --locked`: 54 tests pass.
- `python3 -m unittest discover -s tests`: 273 tests pass.
- Vendored snapshot pinned to `12f9f78` (`tools/check-vendor.py`).
- Desktop-contract and GUI smoke harnesses pass against the demo service.
- Live end-to-end `Start`/`Stop` verified on real hardware (AP comes up on the
  adapter while the upstream STA stays connected; clean teardown, no leftover
  interfaces or NetworkManager profiles). `ExecStopPost` recovery confirmed in
  the journal.

## Known open gates (not blockers, tracked in docs/backend-audit.md)

These remain conservative refusals or pending live validation and are called out
so downstream packagers are not surprised:

- Automatic suspend/resume: an authorized request is retained through logind
  sleep, but live hardware validation is pending.
- `dnsmasq` privilege dropping, mandatory access control (SELinux/AppArmor),
  and DHCP/DNS/NAT policy integration.
- Distribution firewall-policy integration (no global firewalld rewrite).
- Install/upgrade/rollback and multiple-driver checks remain gates.
- Legacy factory-password rotation and installer repair hooks are separate work.
- Configuration migration requires explicit existing credentials; fresh
  credentials are random.

Configuration and service startup are explicit administrator steps; the package
does not auto-enable or auto-start sharing.
