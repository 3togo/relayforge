# RelayForge 0.1.1 (2026-10-29)

0.1.1 adds packaging hardening on top of the 0.1.0 native release: AppArmor
confinement, install/upgrade/rollback maintainer scripts, and firewalld
integration (including permanent owned rules).

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

## 0.1.1 hardening

- **AppArmor (MAC)**: ship `packaging/apparmor/usr.lib.relayforge.relayforge-service`
  and load it in complain mode from `postinst` until enforcement is validated
  against live hardware. The profile is a conffile.
- **install/upgrade/rollback**: add `postinst`/`prerm`/`postrm` that reload systemd,
  enable (do not auto-start sharing) the unit, restart on upgrade only when already
  running, and stop on removal.
- **firewalld integration**: the firewall executor now routes the same owned iptables
  rules through `firewall-cmd --direct --passthrough ipv4` when firewalld is active,
  so distribution firewall policy is respected instead of being clobbered on the next
  firewalld reload. Owned rules are also written to firewalld's **permanent** direct
  passthrough (best-effort; the runtime rule stays authoritative) so they survive a
  firewalld reload instead of being flushed. No global firewalld rewrite is performed.
- **AppArmor child profiles**: the service profile now defines dedicated `hostapd` and
  `dnsmasq` child profiles (still `flags=(complain)`) and transitions the spawned AP
  daemons into them via `Px`, replacing the earlier `Ux` (unconfined) transitions. This
  closes the child-daemon gap noted in the profile header and prepares for enforcement.
- `tools/build-deb.py` packages the above and remains lintian-clean for both backends.

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
- `dnsmasq` privilege dropping is implemented; DHCP/DNS/NAT policy integration
  and SELinux remain pending. AppArmor ships in complain mode with confined
  `hostapd`/`dnsmasq` child profiles (enforcement pending).
- Distribution firewall-policy integration: firewalld passthrough **and** permanent
  owned rules are integrated; dedicated firewalld zones remain pending.
- Install/upgrade/rollback maintainer scripts are added; multi-driver and
  cross-distro validation remain gates.
- Legacy factory-password rotation and installer repair hooks are separate work.
- Configuration migration requires explicit existing credentials; fresh
  credentials are random.

Configuration and service startup are explicit administrator steps; the package
does not auto-enable or auto-start sharing.
