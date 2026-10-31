# Native packaging and desktop qualification

Task 18 passed on 2026-10-23 (UTC), using a disposable x86_64 KVM guest on
Ubuntu 26.10 (Stonking Stingray). The guest ran systemd 261.3, D-Bus 1.16.2,
Polkit 127, GNOME Shell 51.0, GTK 4.24.1 and GTK 3.24.52. It had no wireless
radios. Rendering used software and virtual displays. No hotspot was started.
The build environment was Ubuntu 26.10, Rust/Cargo 1.97.1 and GCC 15.3.0.

## Changes established by this qualification

Real D-Bus activation exposed a first-call race: the service claimed its name
while exporting only an initialization interface. An immediate caller received
`UnknownInterface`. The complete hotspot interface is now registered before
name acquisition. Requests wait for recovery and the initial power handshake;
exclusive name ownership still prevents a second supervisor from recovering
resources concurrently. A private-bus regression holds the initial sleep
snapshot and proves that an early Start waits and then respects suspended state.

Tray password prompts are now controlled explicitly. Start/Stop use the
separate `.control` Polkit action, permitting an active local console user to
control the configured hotspot without an administrator password. Other
sessions receive no such grant. Credential access and configuration changes
retain `.manage` authorization. The service forwards interactive authorization
only when the caller explicitly sets the D-Bus flag; routine tray calls do not.

Settings opens locked without calling GetConfig or reading local credential
files. **Unlock settings** is an explicit interactive request. Automatic saves
remain noninteractive; an authorization failure offers **Authorize and retry
save**. About has no configuration/service dependency. Denied tray operations
retain visible guidance. The maintained policy and the deployment payload's
reviewed-template check now reference the same file in `packaging/`; the pinned
vendor policy remains unchanged.

## Source and artifacts

The tested candidate is based on commit
`57c9980084570668577228c324c7a8ea1842f73e`, with source-receipt SHA-256
`8b0b11fc8c43132b17ca0612417e3c11a2d53f6fbdf431640c10e4576895af55`.
The two unrelated local deployment edits were excluded from that candidate.

The build source ID is
`85e106df770fb3a46dcc001f9600db6cc9dbe5bbc81de31585cfdd3d097d5f3b`;
the release service SHA-256 is
`fc56f938544add5860d6215eb213c2146c69dab065d86494b5ef65bca8444fee`.
The actual VM packages use `0.1.0~system1` and `0.1.0~system2` fixture versions,
not published releases. All nine package hashes, sizes, dependencies, final
harness hashes, exact OS/package versions and check names are recorded in
[`native-system-integration-validation.json`](native-system-integration-validation.json).
This report describes that historical candidate. The current tree also includes
incoming AP lifecycle and package maintainer-hook changes plus the merged desktop
adapter. It no longer matches that tested receipt; the final merged tree requires
a fresh disposable OS qualification before transferring these conclusions.

The original Ubuntu cloud image was the 20261019 amd64 build, SHA-256
`a021682aff9c0d455315230f229138e4d1413c5bce96ec336e453a69086d9c2f`.
Its SHA256SUMS signature verified with the distribution's cloud-image keyring.
The final run reused dependencies in disposable derived guest disks; every
backing image hash is retained in the private evidence. Apt used its normal
resolver, without forced dependencies or a private dpkg root.

## Passing checks

All 36 offline stages passed: 60 Rust tests, 329 Python tests, 67 editor/desktop
tests, the Node Shell-controller checks, optimized and ASan applet checks, and
vendor/integration provenance. Four isolation-guard tests also passed against
the final frozen harness. No tests were skipped.

The final real-OS run completed all 32 checks:

- Install, idle upgrade and rollback of create_ap packages, followed by explicit
  administrator selection of the NetworkManager backend. Apt preserved private
  configuration, customized tray autostart and user preferences.
- No installation-triggered service activation. An immediate GetStatus activated
  the correct systemd unit and returned a valid idle response for every version.
- Real Polkit allowed root credential reads and denied an unprivileged caller.
  A real local tty session on seat0 obtained `.control` authorization without
  interaction. Noninteractive `.manage` returned an authentication challenge,
  rather than invoking an agent.
- The maintained GNOME extension reached `ACTIVE` in Shell 51. Settings and
  About mapped visible windows; the tray remained running on a private Xvfb
  display. The guest system-bus trace contained GetStatus/GetCapabilities and
  no GetConfig/SetConfig while opening these controls.
- Companion removal/reinstallation retained the base service and customized
  preferences. Base removal and real dpkg purge removed owned desktop files and
  autostart configuration while preserving explicitly initialized private
  hotspot configuration and user preferences. Dpkg handled purge because apt
  cannot resolve a fully removed, local-only companion by package name.

Initial failing runs are retained as diagnostic evidence, not passing results.
They exposed the activation race, reviewed-policy template mismatch and a
source-layout import error; all were corrected. Fixture issues involving the
local tty session, pkcheck challenge exit code, purge lookup and one script
updated during execution were corrected before the final frozen run.

## Isolation and remaining gates

[`tools/system-validation/README.md`](../tools/system-validation/README.md)
describes the reproducible harness. QEMU ran as an ordinary host user with a
private writable overlay, no device/bus/display/filesystem passthrough, and one
SSH forward bound to loopback. There was no TAP or host bridge setup. Guest
package installation and systemd/PAM operations occurred only after the guest
marker, virtualization and zero-radio checks. Each VM was powered off after
its run. Private keys are excluded from retained evidence exports.

Read-only host device-state and active-connection timestamp inventories matched
exactly before and after the entire validation. The active Wi-Fi was untouched.
The unrelated local deployment changes remained byte-identical. No candidate
package was installed on the live Wi-Fi host and no source was committed or
pushed during this task.

This qualifies the tested Ubuntu 26.10/Shell 51 idle lifecycle and desktop
matrix. It does not qualify active-hotspot upgrades, older Shell/distribution
versions, interactive Unlock-agent completion, physical radio/driver behavior,
DHCP/DNS/client traffic, bridge traffic, or suspend/recovery on real hardware.
Former combined-package migration retains its separate offline file-lifecycle
evidence. Tasks 19–20 and the remaining release gates stay open.
