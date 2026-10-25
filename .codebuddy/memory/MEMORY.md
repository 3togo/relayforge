# RelayForge — durable project facts

## What this project is
A Rust migration of `~/git/gnome-wifi-hotspot` (Wi-Fi Relay). Rust owns the
system D-Bus service `io.github.erhanzeyrek.WifiHotspot`, Polkit authorization,
configuration/credential storage, radio inspection, and two opt-in native
backends (`networkmanager`, `create_ap`). The GTK settings app, GNOME Shell
extension and desktop tray are **vendored verbatim**, not ported.

## Running the tests (important)
- `cargo test --workspace --locked`, `python3 tools/check-vendor.py`
- **The checkout must not be group/world-writable.** `ap_process::trusted_file`
  and `deployment/fs.rs` reject `mode & 0o022 != 0`, and this machine's checkout
  lands 0664/0775. `umask 0022` only affects new files, so repair the tree once:
  `chmod -R g-w,o-w .` (git tracks no such bits; vendor still verifies).
  Then `umask 0022 && dbus-run-session -- python3 -m unittest discover -s tests`
- **`test_deployment.py` / `test_live_deployment.py` need an up-to-date release
  build.** They stage the service executable as a deployment payload and the
  engine refuses files > 64 MiB; the debug binary is ~104 MB with full symbols.
  `tests/test_dbus._binary()` picks `target/release/relayforge-service`
  automatically when it is at least as new as the debug binary and the debug one
  is oversized, else `RELAYFORGE_BINARY` overrides. A stale release build makes
  these 36 tests fail with "Unsafe or oversized deployment file" — rebuild
  release, that is not a code regression.
- Individual modules: `dbus-run-session -- python3 -m unittest discover -s tests -p 'NAME.py'`
- Full suite: 270 tests, all green on 2026-10-26 (Rust 54 unit tests).

## Conventions
- `vendor/wifi-relay` must stay byte-identical to revision
  `12f9f78e2cf4e9f41abacf94fbf5f5524bf47814`; `tools/check-vendor.py` enforces it.
- Helper/daemon output is **untrusted**: never put it in a D-Bus reply. Publish
  redacted diagnostics to stderr/journal instead (see `tests/test_create_ap.py`
  `flood` case, which asserts `credential-must-not-leak` never appears).
- Credentials never go into argv, and secrets are stripped from diagnostics.
- Native modes are deliberately conservative: bridge mode, `COUNTRY` overrides,
  upstream band switching during `Start`, and any drift from the frozen radio
  channel are refusals, not silent adaptations. They are covered by tests.
- New native capability ⇒ new module in `crates/relayforge-service/src/`, a doc
  under `docs/native-*.md`, and a fixture-backed test module in `tests/`.
- Operations broadcast transitional `StatusChanged` (`connecting`/`stopping`)
  **before** the backend runs and refresh after (pinned-dispatcher contract).
- `ExecStopPost` units re-invoke `relayforge-service <mode> --recover-all`:
  journal-scoped teardown recovery that skips config/BACKEND gates (mirror of
  the pinned `nm_backend.py --recover-all`).
- `deployment.rs` `integration()` templates must byte-match what
  `tools/stage-native.py` stages; changing a staged integration file without
  updating the `include_bytes!` template fails every deployment test.

## Layout
- `crates/relayforge-core/` — no I/O: config schema, radio parsing, lifecycle, subnet.
- `crates/relayforge-service/` — zbus service, `networkmanager.rs`, `create_ap.rs`,
  `ap_runtime/` (firewall, network, daemons, instance, radio plan), `nm_journal.rs`,
  `power.rs` (logind), `deployment/`, `band_switch.rs`, `radio_watch.rs`, `stale.rs`,
  `config_store.rs` (also owns `repair_interface`), `inventory.rs` (`unique_wifi`).

## Deliberate divergences from the pinned source (do not "fix")
- `GetInterfaces` returns true/empty sysfs lists, never invented `["wlan0"]` fallbacks.
- Native modes refuse bridge, `COUNTRY`, `FIX_UNMANAGED`, upstream band switching in
  `Start`, and `nf_nat_pptp` module loading.
- `--init-config` does not inspect adapters unless `--detect-interface` is given.
- The legacy interface repair is an explicit command
  (`--repair-config-interface PATH [--wifi-interface NAME]`), not a package hook;
  `--test-*` variants take a fixture `NET_ROOT` instead of `/sys/class/net`.
- The GNOME extension and `integration/` NM patches are not shipped; the
  `native_wifi_relay` NM capability (`0x7001`) is intentionally not used.
- Native monitoring emits `UserActionRequired({"type":"error"})` once per distinct
  backend error (`Service::announce_error`), mirroring the pinned poll.
- `compat/worker.py` — private pipe adapter loading the pinned Python engine.
- `desktop/tray.py` — maintained fork of the vendored tray.
