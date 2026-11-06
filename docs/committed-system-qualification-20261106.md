# Committed package lifecycle qualification

The later [669175f qualification](qualification-669175f-20261106.md) records a
fresh 39-check installed-system rerun and physical Wi-Fi/suspend validation.
The results below remain evidence for the earlier package-hook commit.

On 2026-11-06 (UTC), packages from committed source
`e2a0ebc346930e48d5b67d1d66c90cdc13e1a44d` passed all 39 installed-system
checks in a fresh disposable Ubuntu 26.10 KVM overlay. In particular, removing
either running idle backend stopped its service and removed its D-Bus owner.
This qualifies the committed package-hook fixes rather than the earlier
uncommitted candidate. The [receipt](committed-system-qualification-20261106.json)
records the source, package hashes, check names and environment.

The hooks preserve administrator enablement choices, stop and purge both
backend units, and consider either already running backend for upgrade restart.
Three focused hook regression tests passed both locally and remotely; shell
syntax checks passed for all three hooks. Install, upgrade, rollback, backend
replacement, D-Bus/systemd activation, authorization, GNOME controls, companion
package handling, purge and configuration preservation passed in the VM.

All nine fixture packages were newly built from the exact committed snapshot.
The guest verified their SHA-256 hashes against the host manifest before package
operations, and its completed report matched that manifest. Each base package's
three extracted hooks matched the committed files. All 279 committed files and
their permissions matched before and after testing.

The service executable was reused from refactor commit
`127b59cff5531c38481993f05b8118cd3d3f9d64`, whose only subsequent changes in
the tested package commit were the three hooks and their regression test.
This was not a fresh Rust build. The frozen external harness exactly matches
`tools/system-validation/{run.py,guest.py,desktop.sh,probe.js}` being published
with this report; all four hashes are in the receipt. Documentation and test
publication does not imply that package tests were rerun at its later commit.

The host ran Ubuntu 26.10 (Stonking Stingray), x86-64, with Python 3.14.8.
The Ubuntu 26.10 guest ran systemd 261.3, D-Bus 1.16.2, Polkit 127, GNOME Shell
51.0, GTK 4.24.1 and 3.24.52, and Python 3.14.7. The retained image chain matched
all six reference hashes and passed fresh signed-checksum verification.

The VM ran as an ordinary user in an isolated container with KVM access. No host
network interface, system bus, physical radio or USB device was passed into the
guest. Its QEMU process and container stopped after testing. Host networking
and source contents were unchanged, and no candidate package was installed on
the development host. Private evidence is retained outside Git; its archive hash
is in the receipt. Guest disks, keys and network inventories are not published.

These checks cover idle services, not active-sharing upgrades, physical-radio
behavior, interactive Polkit-agent prompts, every desktop version or other
operating systems. The earlier [merged-source qualification](merged-system-qualification-20261031.md)
remains separate historical evidence.
