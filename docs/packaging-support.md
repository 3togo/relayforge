# Packaging scope and remaining system checks

The maintained native package builder targets Debian-format distributions.
Assembly, normal apt resolution, idle lifecycle and desktop/system integration
have been validated in an isolated Ubuntu 26.10 x86_64 guest with Shell 51; see
[native-system-integration-validation.md](native-system-integration-validation.md).
This does not certify compatibility with older distribution releases. Build the service on the intended target distribution;
do not relabel an ELF from a newer distribution.

The builder emits `relayforge` and its recommended exact-version
`relayforge-tray` companion. `--with-gnome` additionally emits an exact-version
`relayforge-gnome` companion for the declared Shell range. Tray-only libraries,
launchers and login files belong to the tray package. Shared desktop helpers and
the settings editor remain in the base package. Neither companion enables
sharing or installs service restart hooks. Saved user preferences are not owned
by any package.

The former combined package is covered by an isolated upgrade fixture. Tray
`Breaks`/`Replaces` metadata transfers former base-package files during upgrade;
the fixture checks custom autostart preservation across the transfer and later
remove/reinstall/purge. These tests force dependencies in a private dpkg root.
That former-package migration fixture alone does not establish real dependency
resolution or session behavior. With `--files-only`, the fixture records original
package hashes, removes hooks/triggers only from temporary copies, and tests file
ownership using those copies. Original packages remain untouched; maintainer-hook
behavior needs the separate disposable OS harness. The current split-package lifecycle has separate
real-OS evidence in task 18. Interactive agent completion and active-hotspot
upgrades remain unqualified.

The reference README at revision `12f9f78` explicitly excludes RPM builds from
its validated release. No RPM target distribution or supported toolchain has
been selected for RelayForge. Retaining an untested recipe would not establish
support. RPM work remains deferred pending an explicit target; no RPM parity or
support claim is made.

Downstream applet, control-center and NM integrations are also separate from
native package support. The build-only wrapper checks the changelog baseline and
applies reviewed patches with zero fuzz to a fresh copy. A changelog match is not
proof of pristine source provenance: obtain the matching authenticated
distribution source and preserve its checksums before a full build. Full builds,
dependency verification and installed integration checks remain open.

AppStream and About metadata retain the existing desktop identity for
compatibility, use RelayForge links, and avoid original-project release history.
The two manual pages describe only the current supported entry points and limits.
Source archives export a committed tree and record its identity; they do not
contain workspace edits, Git history or validation artifacts, and do not replace
secret, provenance or redistribution audits.

Tray controls use a separate Polkit action: an active user on the local console
may start or stop the already configured hotspot without an administrator
password. Inactive, remote and sessionless callers receive no such grant.
Reading/changing saved credentials and other privileged operations retain the
management action. The native payload installs the maintained policy from
`packaging/`, leaving the pinned vendor policy unchanged.

The service honors D-Bus `AllowInteractiveAuthorization`: normal tray calls,
background refresh, About, and opening Settings never request an agent prompt.
Settings opens locked and does not call `GetConfig` until the user explicitly
chooses **Unlock settings**. Failed automatic saves remain noninteractive;
**Authorize and retry save** is an explicit authorization action. Successful
management authorization is retained briefly by Polkit, subject to local
administrator policy. A denied tray action displays guidance instead of asking
for a password. See the [Polkit authority interface](https://www.freedesktop.org/software/polkit/docs/latest/eggdbus-interface-org.freedesktop.PolicyKit1.Authority.html)
and [action defaults](https://www.freedesktop.org/software/polkit/docs/latest/polkit.8.html).

Stop sharing and stop the selected service before upgrading, replacing its
backend, or removing the base package. The base package retains the incoming systemd/AppArmor maintainer hooks. Its
post-install hook can restart an already running NetworkManager-backend service
on upgrade; the companion packages have no hooks. The file-lifecycle fixture
checks copies with hooks removed and does not qualify those service side effects.
No configuration rewrite hook is installed. Changing backend packages preserves
the administrator's configuration; select the matching `BACKEND` explicitly
after confirmed cleanup. The disposable VM harness is documented in
[`tools/system-validation/README.md`](../tools/system-validation/README.md).
