# RelayForge — durable project facts

*(Consolidated 2026-10-26.)*

## User preferences
- Write all memory entries in **English**.
- **Always use the `gh` CLI** for git/GitHub operations (repos, remotes, branches, PRs, releases).
  `gh` is logged in as `3togo`, protocol ssh, scopes `repo workflow read:org`.
- **Git conflicts are mine to resolve, never the user's.** If an untracked local file collides
  with a tracked file about to be checked out (typical: `.codebuddy/memory/`), stash it aside,
  act, then append the content back. **Never delete it.**
  ```bash
  git stash push -u -m "local notes" -- .codebuddy/memory
  git merge --ff-only main
  git show 'stash@{0}^3:.codebuddy/memory/MEMORY.md'   # untracked files live in the 3rd parent
  # merge both sets of notes, then: git stash drop
  ```
- `.codebuddy/memory/` is committed in `main`. To exclude it from a commit:
  `git add -A -- . ':(exclude).codebuddy'`.
- **Never post, push, open a PR/issue, or comment on `amule-org/amule` without explicit
  permission** (user, 2026-10-26). Nothing has ever been sent there from this workspace.

## What this project is
A Rust migration of the Python project `~/git/gnome-wifi-hotspot` (Wi-Fi Relay, fork of
`erhanzeyrek/gnome-wifi-hotspot`). Rust owns the system D-Bus service
`io.github.erhanzeyrek.WifiHotspot`, Polkit authorization, config/credential storage, radio
inspection and two opt-in native backends (`networkmanager`, `create_ap`). The GTK settings
app, GNOME Shell extension, tray and icons are **vendored verbatim** under `vendor/wifi-relay`
(pinned at legacy HEAD `12f9f78`); `compat/worker.py` still runs the pinned Python engine, and
`desktop/tray.py` is a maintained fork of the vendored tray.
Current version 0.1.0, no published release; the README states native modes are **not yet
validated production replacements**.

## Build and test (important)
- `cargo build --workspace --all-targets --locked`, `cargo test --workspace --locked`,
  `python3 tools/check-vendor.py` (48 files at `12f9f78`).
- **The checkout must not be group/world-writable** — `ap_process::trusted_file` and
  `deployment/fs.rs` reject `mode & 0o022`. This machine lands 0664/0775; `umask 0022` only
  affects new files, so repair once with `chmod -R g-w,o-w .`.
- **`test_deployment.py` / `test_live_deployment.py` (36 tests) need a current release build**:
  the deployment engine refuses payloads > 64 MiB and the debug binary is ~104 MB.
  `tests/test_dbus._binary()` prefers `target/release/relayforge-service` (10 MiB) when it is
  at least as new as the debug one; `RELAYFORGE_BINARY` overrides. Stale release ⇒
  "Unsafe or oversized deployment file" — rebuild, it is not a regression.
- Full Python suite: `umask 0022 && dbus-run-session -- python3 -m unittest discover -s tests`
  (270 tests green on 2026-10-26; Rust 54 unit tests). Per module:
  `... -p 'NAME.py'`.

## Conventions
- Helper/daemon output is **untrusted**: never in a D-Bus reply. Publish a bounded 16 KiB
  redacted tail to stderr/journal (`tests/test_create_ap.py` `flood` proves helper output can
  contain credentials). Credentials never enter argv.
- Native modes are deliberately conservative: bridge, `COUNTRY`, `FIX_UNMANAGED`, upstream
  band switching during `Start`, and drift from the frozen radio channel are refusals, not
  silent adaptations.
- New native capability ⇒ new module in `crates/relayforge-service/src/`, a doc under
  `docs/native-*.md`, and a fixture-backed test module in `tests/`.
- Operations broadcast transitional `StatusChanged` (`connecting`/`stopping`) **before** the
  backend runs, then refresh (pinned-dispatcher contract).
- `ExecStopPost` re-invokes `relayforge-service <mode> --recover-all`: journal-scoped teardown
  recovery that skips config/BACKEND gates.
- `deployment.rs` `integration()` templates must byte-match what `tools/stage-native.py`
  stages, otherwise every deployment test fails.

## Deliberate divergences from the pinned source (do not "fix")
- `GetInterfaces` returns true/empty sysfs lists, never invented `["wlan0"]` fallbacks.
- Shipped Polkit policy is **split** into two actions: `...WifiHotspot.manage`
  (Start/Stop/SwitchBandAndReconnect/PrepareFirewall, `allow_active=yes`) and
  `...WifiHotspot.credentials` (GetConfig/SetConfig, `auth_self_keep`); both
  `allow_any=no`, `allow_inactive=no`. This replaces the pinned upstream `auth_admin`
  policy (`vendor/wifi-relay/data/`) — the user chose "no need to protect the hotspot
  password from other process".
- `--init-config` only inspects adapters with `--detect-interface`; legacy interface repair is
  the explicit `--repair-config-interface PATH [--wifi-interface NAME]` (not a package hook);
  `--test-*` variants take a fixture `NET_ROOT`.
- The `native_wifi_relay` NM capability (`0x7001`) and the `integration/` patched
  nm-applet / nm-core / gnome-control-center debs are intentionally **unshipped**.
- Native monitoring emits `UserActionRequired({"type":"error"})` once per distinct backend
  error (`Service::announce_error`).

## Layout
- `crates/relayforge-core/` — no I/O: config schema, radio parsing, lifecycle, subnet.
- `crates/relayforge-service/` — zbus service, `networkmanager.rs`, `create_ap.rs`,
  `ap_runtime/` (firewall, network, daemons, instance, radio plan), `nm_journal.rs`,
  `power.rs` (logind), `deployment/`, `band_switch.rs`, `radio_watch.rs`, `stale.rs`,
  `config_store.rs` (also `repair_interface`), `inventory.rs` (`unique_wifi`).

## Desktop integration
- GNOME: vendored extension → `usr/share/gnome-shell/extensions/wifi-relay@3togo.github.io/`,
  enabled once at first login by `usr/bin/wifi-hotspot-enable-extension`.
- KDE/XFCE: the same dispatcher execs the StatusNotifier tray; XFCE needs
  `xfce4-statusnotifier-plugin` (Debian `Recommends:`). Tray needs
  `PYTHONPATH=vendor/wifi-relay/settings` when run from the repo.

## Live machine and physical testing (verified 2026-10-26)
- Host: Ubuntu 26.10, mainline kernel `7.2.6-070206-generic`, `iw 6.17`,
  NetworkManager `1.58.1`.
- **One** physical Wi-Fi radio only: `wlo1`, PCI `06:00.0`, *Intel Wi-Fi 7 (802.11be)
  BE20*/AX1775* 2x2* `[8086:272b]`, driver `iwlwifi`, firmware
  `106.c88d33af.0 gl-c0-fm-c0-c106`. The second adapter seen earlier (`wlxe84e0648d7eb`)
  is gone and the name is now `wlo1`, not `wlo2`. Regulatory: country `00` (world).
- Upstream is **wired `eno2`** ("Wired Connection"); `wlo1` is unassociated. No Android
  client and no spare Wi-Fi dongle are available, so a real remote client cannot be used.
- `iw phy` concurrency: `#{managed} <= 2, #{AP} <= 1, total <= 4, #channels <= 1` — so an
  AP *plus* two stations can coexist on one channel, which is the only way to fake a
  client here (an extra managed interface joined to our own AP).
- Installed payload path is **`/usr/lib/relayforge/relayforge-service`** (not libexec, not on
  PATH — invoke by full path); Polkit policy at
  `/usr/share/polkit-1/actions/io.github.erhanzeyrek.WifiHotspot.policy`.
- `pkexec` starts in a different cwd → always pass absolute paths, e.g.
  `DISPLAY=:0.0 pkexec dpkg -i /work/git/relayforge/artifacts/deb/relayforge_0.1.0_amd64.deb`.
- `--init-config` **refuses to overwrite** an existing config ("Configuration already exists;
  left unchanged") — patch it with `sed` under `pkexec` instead.
- PSK rules (`relayforge-core` `config.rs::validate()`): `USE_PSK=1` ⇒ PASSPHRASE must be a
  64-digit hex PSK; `USE_PSK=0` ⇒ 8–63 printable ASCII. `--init-config` defaults to
  `USE_PSK=0` + a random 32-char hex passphrase. The native NM backend forwards
  `PASSPHRASE` verbatim into NM's `802-11-wireless-security.psk`.
- **Blocking finding:** the native `networkmanager` backend *cannot share a wired uplink*.
  `radio.rs::network_manager_gate()` requires `interface_type == "managed" && connected`,
  i.e. the Wi-Fi adapter must already be associated to an upstream AP, plus AP/STA
  concurrency and an AP channel equal to the upstream channel
  ("Connect the selected managed Wi-Fi adapter before sharing"). Legacy 1.0.0-31's
  "2.4 GHz ch12 with Ethernet egress" cohort is therefore **not reproducible on this host**
  with the NM backend; the `create_ap` backend (which NATs from `INTERNET_IFACE=eno2`) is
  the only wired-uplink path. (Correction: it does *not* put hostapd directly on `wlo1` —
  `NO_VIRT=0` makes it create a virtual `rfap<hash>` interface and hostapd runs there.)
- **Second blocking finding (live trial 2026-10-26): the native `create_ap` backend also
  fails on this host.** Pipeline (`ap_runtime/instance_runtime.rs::start`) is
  `radio-check → interface-create → device-release → daemons-configure → interface-configure
  → forwarding/firewall → daemons-run`; it dies at `interface-configure`. The virtual AP is
  created but stays **type managed** and never becomes AP, and Start then reports
  `create_ap startup failed; cleanup unconfirmed and journal retained`. Two distinct errors:
  `AP state changed outside the interface transaction` (`ap_runtime/network.rs::check_current`)
  while NM still managed `wlo1`; `Unjournalled AP identity changed`
  (`network.rs::remove`) once `wlo1` was set unmanaged. `--recover-all` also fails —
  `Radio inspection command failed` — and leaves the stale interface, so cleanup had to be
  done by hand (`iw dev rfap… del` + `rm /run/relayforge/create-ap*`). The lock is held by
  the running service, so `--recover-all` only works when the unit is stopped.
- **Ruled out:** the driver is *not* at fault — on this iwlwifi BE200 both
  `iw phy phy0 interface add X type __ap` and create-then-`iw dev X set type __ap` succeed.
  `hostapd 2.11` and `dnsmasq 2.93` are installed (careful: `command -v hostapd` can print
  MISSING from a user shell even though `/usr/sbin/hostapd` exists). No rfkill, no bridge,
  no upstream/AP interface collision.
- **Operator prerequisites:** relayforge refuses `FIX_UNMANAGED`, so NetworkManager must be
  told to leave the AP radio alone first (`nmcli dev set wlo1 managed no`); otherwise NM's
  autoconnect profiles (`marvel-staff-5G`, `dlink-F3CA`) mutate `wlo1` mid-transaction.
  **The user asked that `wlo1` stay unmanaged (2026-10-26) — do not silently restore it.**
- **Packaging gap:** the deb `Depends` lists `network-manager, iw, iproute2, python3…` but
  **not** `hostapd`, `dnsmasq` or `iptables`, which the create_ap backend needs. Both backend
  variants emit the same filename `relayforge_0.1.0_amd64.deb` (use
  `build-deb.py --backend … --force`), so they are indistinguishable by name.
- **Physical clients available (offered by the user 2026-10-26, over adb):**
  `0123456789ABCDEF` = Alldocube *Kuwan Pad Pro*, Android **14**; `3DP21A0326210257` =
  IVIDEO *3DP21A03*, Android **7.1.1**. Together they cover legacy 1.0.0-31's Android-14 case
  *and* the "other client OSes" gap legacy left untested. Both were associated to
  `marvel-staff-5G`.

## Repository
- `3togo/relayforge` — https://github.com/3togo/relayforge, ssh
  `git@github.com:3togo/relayforge.git` (remote `origin`), default branch **`main`** (never
  create `master`). Not a fork.
- Sync: `git fetch origin` → `git switch main && git pull --ff-only` → rebase topic branch.
- Cleanup done 2026-10-26: all 11 topic branches were merged into `main`, renamed to
  `obsolete/*` and deleted; the remote branch `create-ap/rust-daemon-runtime` was deleted via
  `gh api -X DELETE repos/3togo/relayforge/git/refs/heads/<branch>`. Only `main` exists on
  both sides. Restore an old branch: `git branch obsolete/<name> b64cd7f`.

## Legacy project `~/git/gnome-wifi-hotspot`
- GitHub `3togo/gnome-wifi-hotspot`, a **fork** of `erhanzeyrek/gnome-wifi-hotspot`, public,
  0 stars / 0 forks, **issues disabled**, 13 merged PRs (all self-authored), single branch
  `main` = `origin/main` = `12f9f78`. Last release `v1.0.0-31` (2026-10-19, `bundle.deb`),
  last push 2026-10-21. The `upstream` fetch-only remote documented in its
  `docs/fork-maintenance.md` is **not** configured.
- Retains the package/D-Bus/Polkit identity relayforge targets, so the two stacks are
  **not coinstallable** (same `/etc/wifi-hotspot.conf`, bus name, action ids).
- Artifacts relayforge does **not** carry: `packaging/` (Debian control, rpm spec,
  `minimize-integrations.py`, `start-tray-sessions.py`), `integration/` (NM patches),
  the legacy `tests/` suite, `docs/releases/` + `docs/evidence/` provenance, RELEASE_NOTES.md.

## Hard-won lessons
- **Fixture-backed tests only prove the fixture.** `psk-flags=2` (`NOT_SAVED`) made every
  native NM start fail with `no-secrets`; an assertion matching the implementation instead of
  NM's semantics enshrined it. Anything touching real NetworkManager needs one run on the
  real bus.
- Missing config must exit `17` (`EXIT_MISSING_CONFIG`); units use
  `RestartPreventExitStatus=17`, `StartLimitIntervalSec=300`, `StartLimitBurst=10`, otherwise
  systemd restart-loops (hit live: 354 restarts). After a loop, `systemctl reset-failed`
  before starting.
- `sudo` has no cached credential for the agent, but `pkexec` works on `DISPLAY=:0.0`:
  `DISPLAY=:0.0 pkexec /bin/sh -c '...'`.
- The vendored GTK app is upstream code: GUI bugs are upstream bugs, but smoke it anyway
  (`app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)` first in demo mode, because the app id
  equals the service name).
- `Gio.DBusConnection.signal_subscribe` without `user_data` passes **6** args, not 7; bind
  with `*args` or GDBus swallows the callback silently.
- Hardware facts live in "Live machine and physical testing" above — the previously noted
  `wlo2` (2.4 GHz only) + `wlxe84e0648d7eb` pair is **stale**: the host now has a single
  `wlo1` (Intel Wi-Fi 7) and a wired uplink.

### Current physical Wi-Fi ownership preference (2026-10-26)
The user now explicitly wants hotspot enable/disable to leave physical Wi-Fi managed.
This supersedes the earlier instruction to leave wlo1 unmanaged. Restored wlo1 with
`nmcli device set wlo1 managed yes`; its autoconnect setting was left unchanged.
Two installed-service hotspot cycles passed with wlo1 managed in all 50 samples.
Only the temporary rfap* AP is unmanaged while RelayForge owns it; it is deleted
on stop. Evidence: artifacts/wifi-managed-20261026/results.json.

### Desktop and AP fixes completed later on 2026-10-26
Current implementation supersedes older integration exclusions above:
- Optional `--desktop-integrations both` builds exact-version patched nm-applet
  and GNOME Settings packages; the generated RelayForge deb requires them.
  Both integrations were installed and exercised in the XFCE session.
- Maintained settings now live in desktop/main.py; packaging overlays this and
  device_info.py/adapter_limits.py onto the unchanged pinned settings snapshot.
  Device selectors show connection type and hardware details, hide rfap children,
  and query every listed Wi-Fi adapter's NetworkManager AP capability. Unsupported
  drivers are grayed out for hotspot use but remain Internet source choices.
- The USB adapter wlxe84e0648d7eb uses mt7601u, reports only 2.4 GHz and lacks AP
  mode. Its former 5 GHz-only exception was removed; capability checks are general.
- Tray menus persist across status polls; widget updates no longer dismiss them.
- AP runtime fixes explicitly release newly discovered virtual devices from NM,
  accept non-Ethernet identities for forwarding inventory, and safely retire
  confirmed stale hostapd sockets during cleanup.
Validation evidence is retained in ignored artifacts/ folders. Hardware smoke
passed on both Android devices before and after AP restart. The final native
build passed 56 Rust tests; original full installed Python validation passed 278
before the later desktop work. Later changes passed focused unit/packaging tests
and 22 installed GUI checks, with live menu/capability checks where hardware was
available. Do not describe all 278 tests as a run of the final desktop revision.
