# Merged-source packaging and desktop qualification

On 2026-10-31 (UTC), the merged native service and corrected package hooks passed
39 real-system checks in a disposable Ubuntu 26.10 (Stonking Stingray), x86-64
KVM guest with GNOME Shell 51. Installation, idle upgrade, rollback, backend
replacement, authorization, desktop startup and removal were exercised with
actual release packages. The guest had no wireless radios; no hotspot was started.

This qualification updates the older [task 18 evidence](native-system-integration-validation.md).
Exact package hashes, source identities, tool versions and check names are in the
[machine-readable receipt](merged-system-qualification-20261031.json).

The later [committed-package qualification](committed-system-qualification-20261106.md)
records the 2026-11-06 rerun on commit e2a0ebc. This report and its receipt retain
the historical candidate identities below.

## Corrections and regression coverage

The package hooks previously enabled a disabled NetworkManager service during
installation or upgrade, contradicting the documented administrator-controlled
startup policy. They also stopped and purged only the NetworkManager unit,
leaving the create_ap backend outside removal handling. The hooks now preserve
enablement choices and handle both backends. Upgrade restart handling also covers
either already running backend; users must still stop sharing before upgrading.

The VM harness had a separate provenance defect. Copying a package directory into
a reused guest's existing directory nested the new input beneath the old one.
Tests consequently consumed retained fixture packages. The runner now uses a
fresh destination for each run, supplies a SHA-256 manifest, and requires the
guest to verify every package before package operations. The completed guest
report must match that manifest too. A regression rejects mismatched package
contents before any package commands execute.

The final guest checked that fresh installations leave the selected service
disabled, an administrator's explicit enable survives upgrade, and a subsequent
disable survives rollback. It removed each backend while its service was running
idle, verified that the unit stopped and the D-Bus owner disappeared, and checked
preservation of private configuration and desktop preferences.

Two exploratory VM runs consumed retained fixture packages and are excluded from
qualification. Their apparent results do not establish current-source behavior.
The final run used all nine intended packages, verified inside the guest.

## Results and environment

| Check performed in this run | Result |
| --- | --- |
| Locked, offline release workspace build, including all targets | Passed |
| Release Rust workspace tests | 73 passed; no failures or ignored tests |
| Focused package-hook, archive, file-lifecycle and guest-safety tests | 14 passed; no failures or skips |
| Disposable real-system and desktop qualification | 39 checks passed |
| Guest package identities against the host manifest | All nine matched |

The build machine ran Ubuntu 26.10, x86-64, with Rust/Cargo 1.97.1. Build caches
were reused, and the fresh checkout's Rust crates were recompiled before testing.
The unchanged Rust executable was reused when packaging the hook corrections.
Focused Python tests ran in a disposable Ubuntu 26.10 container with networking
disabled. The full Python/desktop offline suite was not repeated in this run;
earlier commit validation remains separate evidence.

The guest ran systemd 261.3, D-Bus 1.16.2, Polkit 127, GNOME Shell 51.0,
GTK 4.24.1, GTK 3.24.52 and Python 3.14.7. Settings and About mapped visible
windows, the tray remained running, and the GNOME extension became active.
Desktop startup did not request protected configuration. Real Polkit allowed
root credential access, denied an unprivileged caller, and granted local active
console control while management still required authentication.

The VM ran as an ordinary user inside a disposable container with KVM access.
It used a private writable overlay and software displays, without host network
interfaces, filesystems, system buses, USB devices or physical radios passed into
the guest. The VM powered off and its container exited after qualification.
Host addresses, routes and active connection identities matched before and after;
raw address snapshots differed only in elapsed lease lifetimes. No candidate
package was installed on a development host.

## Source and retained evidence

The tested base is `d62bce8bad87234e4f8ebab903966afb7e382ea8`, tree
`360b613e3d4214b406fe7967475f8634035dd9f2`, plus the implementation patch with
SHA-256 `90b028147f863e906e9172033a8fff5ed861b9cb30c3899b15db3038bc5d6818`.
The additional test file, `tests/test_package_hooks.py`, has SHA-256
`d0b242d0166be1fa5c37ea5e8c8c1e59e68f81352601db0d307988122863706f`.
Documentation ledger updates were added after this tested patch.

The release executable before package stripping has SHA-256
`b6dc8ec1190e0e4eb227b03c29e958efd2e7cf349240ab4b5b670d94ea444fe7`.
The nine packages use fixture versions `0.1.2~qualification1` and
`0.1.2~qualification2`; they are not published releases.

The reused disposable image chain is recorded by digest in the receipt. Its
original Ubuntu cloud image still matched SHA-256
`a021682aff9c0d455315230f229138e4d1413c5bce96ec336e453a69086d9c2f`;
the retained checksum signature verified with the distribution cloud-image keyring.
Each run froze its harness before boot.

Private logs, frozen harness, tested patch, per-file source hashes and network
observations are retained outside Git. A local copy is under
`artifacts/system-qualification-20261031/`. The evidence archive SHA-256 is
`522e9f202ebd28a3019383f0aea4508d028666772a332f15e484f35222b7f4cb`.
Guest keys and disks are excluded from that archive.

Physical client association, DHCP/DNS/HTTPS traffic, driver behavior, active-sharing
upgrades, real suspend/recovery, interactive Unlock-agent completion and other
distribution/Shell versions remain unqualified. Tasks 19–20 remain open; this
report does not establish production hotspot reliability.
